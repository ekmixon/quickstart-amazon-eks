import json
import logging
import boto3
import subprocess
import shlex
import re
import requests
from ruamel import yaml
from datetime import date, datetime
from crhelper import CfnResource
from time import sleep
from uuid import UUID

logger = logging.getLogger(__name__)
helper = CfnResource(json_logging=True, log_level='DEBUG')

try:
    s3_client = boto3.client('s3')
    kms_client = boto3.client('kms')
    ec2_client = boto3.client('ec2')
    s3_scheme = re.compile(r'^s3://.+/.+')
except Exception as init_exception:
    helper.init_failure(init_exception)


def s3_get(url):
    try:
        return s3_client.get_object(
            Bucket=url.split('/')[2], Key="/".join(url.split('/')[3:])
        )['Body'].read().decode('utf8')
    except Exception as e:
        raise RuntimeError(f"Failed to fetch CustomValueYaml {url} from S3. {e}")


def http_get(url):
    try:
        response = requests.get(url)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Failed to fetch CustomValueYaml url {url}: {e}")
    if response.status_code != 200:
        raise RuntimeError(
            f"Failed to fetch CustomValueYaml url {url}: [{response.status_code}] "
            f"{response.reason}"
        )
    return response.text


def run_command(command):
    retries = 0
    while True:
        try:
            try:
                logger.debug(f"executing command: {command}")
                output = subprocess.check_output(shlex.split(command), stderr=subprocess.STDOUT).decode("utf-8")
                logger.debug(output)
            except subprocess.CalledProcessError as exc:
                logger.error(
                    f'Command failed with exit code {exc.returncode}, stderr: {exc.output.decode("utf-8")}'
                )

                raise Exception(exc.output.decode("utf-8"))
            return output
        except Exception as e:
            if 'Unable to connect to the server' not in str(e) or retries >= 5:
                raise
            logger.debug(f"{e}, retrying in 5 seconds")
            sleep(5)
            retries += 1


def create_kubeconfig(cluster_name):
    run_command(f"aws eks update-kubeconfig --name {cluster_name} --alias {cluster_name}")
    run_command(f"kubectl config use-context {cluster_name}")


def json_serial(o):
    if isinstance(o, (datetime, date)):
        return o.strftime('%Y-%m-%dT%H:%M:%SZ')
    raise TypeError("Object of type '%s' is not JSON serializable" % type(o))


def write_manifest(manifest, path):
    with open(path, "w") as f:
        f.write(json.dumps(manifest, default=json_serial))


def generate_name(event, physical_resource_id):
    manifest = event['ResourceProperties']['Manifest']
    if type(manifest) == str:
        manifest = yaml.safe_load(manifest)
    stack_name = event['StackId'].split('/')[1]
    if (
        "metadata" in manifest.keys()
        and 'name' not in manifest["metadata"].keys()
        and 'generateName' not in manifest["metadata"].keys()
    ):
        if physical_resource_id:
            manifest["metadata"]["name"] = physical_resource_id.split('/')[-1]
        else:
            manifest["metadata"]["generateName"] = f"cfn-{stack_name.lower()}-"
    return manifest


def build_output(kube_response):
    return {
        key: kube_response["metadata"][key]
        for key in ["uid", "selfLink", "resourceVersion", "namespace", "name"]
        if key in kube_response["metadata"].keys()
    }


def traverse(obj, path=None, callback=None):
    if path is None:
        path = []

    if isinstance(obj, dict):
        value = {k: traverse(v, path + [k], callback)
                 for k, v in obj.items()}
    elif isinstance(obj, list):
        value = [traverse(obj[idx], path + [[idx]], callback)
                 for idx in range(len(obj))]
    else:
        value = obj

    return value if callback is None else callback(path, value)


def traverse_modify(obj, target_path, action):
    target_path = to_path(target_path)

    def transformer(path, value):
        return action(value) if path == target_path else value

    return traverse(obj, callback=transformer)


def traverse_modify_all(obj, action):

    def transformer(_, value):
        return action(value)
    return traverse(obj, callback=transformer)


def to_path(path):
    if isinstance(path, list):
        return path  # already in list format

    def _iter_path(inner_path):
        indexes = [[int(i[1:-1])] for i in re.findall(r'\[[0-9]+\]', inner_path)]
        lists = re.split(r'\[[0-9]+\]', inner_path)
        for parts in range(len(lists)):
            yield from lists[parts].strip('.').split('.')
            if parts < len(indexes):
                yield indexes[parts]
            else:
                yield []

    return list(_iter_path(path))[:-1]


def set_type(input_str):
    if type(input_str) == str:
        if input_str.lower() == 'false':
            return False
        if input_str.lower() == 'true':
            return True
        if input_str.isdigit():
            return int(input_str)
    return input_str


def fix_types(manifest):
    return traverse_modify_all(manifest, set_type)


def enable_proxy(proxy_host, vpc_id):
    configmap = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": "proxy-environment-variables",
                "namespace": "kube-system"
            },
            "data": {
                "HTTP_PROXY": proxy_host,
                "HTTPS_PROXY": proxy_host,
                "NO_PROXY": "localhost,127.0.0.1,169.254.169.254,.internal"
            }
        }
    cluster_ip = run_command(
        "kubectl get service/kubernetes -o jsonpath='{.spec.clusterIP}'"
    )
    cluster_cidr = ".".join(cluster_ip.split(".")[:3]) + ".0/16"
    vpc_cidr = ec2_client.describe_vpcs(VpcIds=[vpc_id])['Vpcs'][0]['CidrBlock']
    configmap["data"]["NO_PROXY"] += f"{vpc_cidr},{cluster_cidr}"
    write_manifest(configmap, '/tmp/proxy.json')
    run_command("kubectl apply -f /tmp/proxy.json")
    patch_cmd = (
        """kubectl patch -n kube-system -p '{ "spec": {"template": { "spec": { """
        """"containers": [ { "name": "%s", "envFrom": [ { "configMapRef": {"name": """
        """"proxy-environment-variables"} } ] } ] } } } }' daemonset %s"""
    )
    setenv_cmd = (
        """kubectl set env daemonset/%s --namespace=kube-system """
        """--from=configmap/proxy-environment-variables --containers='*'"""
    )
    for pod in ["aws-node", "kube-proxy"]:
        logger.debug(run_command(patch_cmd % (pod, pod)))
        logger.debug(run_command(setenv_cmd % pod))


def handler_init(event):
    logger.debug('Received event: %s' % json.dumps(event, default=json_serial))

    physical_resource_id = None
    manifest_file = None
    create_kubeconfig(event['ResourceProperties']['ClusterName'])
    if 'HttpProxy' in event['ResourceProperties'].keys() and event['RequestType'] != 'Delete':
        enable_proxy(event['ResourceProperties']['HttpProxy'], event['ResourceProperties']['VpcId'])
    if 'Manifest' in event['ResourceProperties'].keys():
        manifest_file = '/tmp/manifest.json'
        if "PhysicalResourceId" in event.keys():
            physical_resource_id = event["PhysicalResourceId"]
        if type(event['ResourceProperties']['Manifest']) == str:
            manifest = generate_name(event, physical_resource_id)
        else:
            manifest = fix_types(generate_name(event, physical_resource_id))
        write_manifest(manifest, manifest_file)
        logger.debug("Applying manifest: %s" % json.dumps(manifest, default=json_serial))
    elif 'Url' in event['ResourceProperties'].keys():
        manifest_file = '/tmp/manifest.json'
        url = event['ResourceProperties']["Url"]
        response = s3_get(url) if re.match(s3_scheme, url) else http_get(url)
        manifest = yaml.safe_load(response)
        write_manifest(manifest, manifest_file)
    return physical_resource_id, manifest_file


def stabilize_job(namespace, name):
    while True:
        response = json.loads(run_command(f"kubectl get job/{name} -n {namespace} -o json"))
        for condition in response.get('status', {}).get('conditions', []):
            if condition.get("status") == "True":
                if condition.get('type') == "Complete":
                    return
                if condition.get('type') == "Failed":
                    raise Exception(f"Job failed {condition.get('reason')} {condition.get('message')}")
        sleep(5)


@helper.create
def create_handler(event, _):
    physical_resource_id,  manifest_file = handler_init(event)
    if not manifest_file:
        return physical_resource_id
    outp = run_command(f"kubectl create --save-config -o json -f {manifest_file}")
    helper.Data = build_output(json.loads(outp))
    if helper.Data.get("selfLink", "").startswith('/apis/batch') and 'cronjobs' not in helper.Data.get("selfLink", ""):
        stabilize_job(helper.Data["namespace"], helper.Data["name"])
    return helper.Data.get("selfLink", physical_resource_id)


@helper.update
def update_handler(event, _):
    physical_resource_id,  manifest_file = handler_init(event)
    if not manifest_file:
        return physical_resource_id
    outp = run_command(f"kubectl apply -o json -f {manifest_file}")
    helper.Data = build_output(json.loads(outp))
    return helper.Data.get("selfLink", physical_resource_id)


@helper.delete
def delete_handler(event, _):
    physical_resource_id,  manifest_file = handler_init(event)
    if not manifest_file:
        return physical_resource_id
    run_command(f"kubectl delete -f {manifest_file}")


def lambda_handler(event, context):
    helper(event, context)
