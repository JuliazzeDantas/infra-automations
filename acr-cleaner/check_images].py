import os
import subprocess
import json
import asyncio
from dotenv import load_dotenv

load_dotenv()

ACR_NAME = os.getenv("ACR_NAME")
WORKSPACE = os.getenv("WORKSPACE")
WORKSPACE_ID = os.getenv("WORKSPACE_ID")
PRD = os.getenv("PRD")
DEV = os.getenv("DEV")
QAS = os.getenv("QAS")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
ACR=os.getenv("ACR")

def getImagesFromLogAnalytics():

    print("Get Images from Log Analytics")

    QUERY='''ContainerRegistryRepositoryEvents
    | where TimeGenerated > ago(60d)
    | where OperationName == "Pull"
    | where UserAgent contains "containerd/"
    | summarize TimeGenerated = max(TimeGenerated) by Repository, Digest
    | sort by TimeGenerated desc'''

    OUTPUT_DIR = "images"
    OUTPUT_FILE = os.path.join(OUTPUT_DIR, "from-kql.json")

    dict_result = {}

    # Ensure the output directory exists
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    JQ_FILTER = 'group_by(.Repository) | map({ (.[0].Repository): map({Digest, TimeGenerated}) }) | add'

    az_cmd = [
        "az", "monitor", "log-analytics", "query",
        "--workspace", WORKSPACE_ID,
        "--analytics-query", QUERY,
        "--output", "json"
    ]

    jq_cmd = ["jq", JQ_FILTER]

    # az ... | jq ... > OUTPUT_FILE
    az_proc = subprocess.Popen(az_cmd, stdout=subprocess.PIPE, text=True)
    jq_proc = subprocess.Popen(jq_cmd, stdin=az_proc.stdout, stdout=subprocess.PIPE, text=True)
    az_proc.stdout.close()  # permite az receber SIGPIPE se jq terminar antes

    result, _ = jq_proc.communicate()

    if jq_proc.returncode != 0:
        raise RuntimeError(f"jq falhou com código {jq_proc.returncode}")
    
    result = json.loads(result)

    for repository, list_digest in result.items():
        # print(repository)
        for digest in list_digest:
            az_comand=["az","acr","repository","show-manifests",
            "--name",f"{ACR}",
            "--repository",f"{repository}",
            "--query",f"[?digest=='{digest["Digest"]}'].tags[]",
            "-o","tsv"]
            tags=subprocess.run(az_comand, capture_output=True, text=True).stdout.split()
            dict_result.setdefault(repository, []).extend(tags)

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        f.write(json.dumps(dict_result, indent=2))


def getImageFromKubernetesManifests():

    print("Get images from K8s")

    type_of_resources_kubernetes = ['deployments', 'jobs', 'statefulset', 'daemonset']
    aks = [PRD, QAS, DEV]

    list_image = []
    dict_image = {}
    string_image = ''

    OUTPUT_DIR = "images"
    OUTPUT_FILE = os.path.join(OUTPUT_DIR, "from-k8s.json")

    # Ensure the output directory exists
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    for cluster in aks:
        for resource in type_of_resources_kubernetes:
            
            az_cmd=['kubectl', f'--context={cluster}', 'get', resource, '--all-namespaces', '-o', 'json']
            if resource == 'cronjobs':
                JQ_QUERY='.spec.jobTemplate.spec.template.spec.containers.[].image'
            else:
                JQ_QUERY = '.items[].spec.template.spec.containers[].image'
            jq_cmd=['jq', '-r', JQ_QUERY]

            az_proc = subprocess.Popen(az_cmd, stdout=subprocess.PIPE, text=True)
            jq_proc = subprocess.Popen(jq_cmd, stdin=az_proc.stdout, stdout=subprocess.PIPE, text=True)
            az_proc.stdout.close()

            result, _ = jq_proc.communicate()

            if jq_proc.returncode != 0:
                raise RuntimeError(f"jq falhou com código {jq_proc.returncode}")
            
            string_image = string_image + result
        
    list_image=string_image.split()
    list_image = [ x.replace('raizenanalyticsdev.azurecr.io/','') for x in list_image if 'raizenanalyticsdev.azurecr.io' in x]
    list_image = list(set(list_image))
    list_image = sorted(list_image)
    for image in list_image:
        print(image)
        item = []
        if "@" in image:
            item = image.rsplit("@", 1)
        elif ":" in image:
            item = image.rsplit(":", 1)
        else:
            item = [image, "latest"]

        repository = item[0]
        tag = item[1]
        dict_image.setdefault(repository, []).append(tag)
    

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    
    with open(OUTPUT_FILE, "w") as f:
        f.write(json.dumps(dict_image, indent=2))

    return dict_image


def getAllImagesFromACR():

    print("Get images from ACR")

    OUTPUT_DIR = "images"
    OUTPUT_FILE = os.path.join(OUTPUT_DIR, "all-acr.json")

    az_cmd=["az","acr","repository","list","--name",f"{ACR}","--output","tsv"]
    result = subprocess.run(az_cmd, capture_output=True, text=True)

    acr_list = result.stdout.split()
    disct_repo_tags = {}

    for repository in acr_list:
        print(repository)
        az_cmd_show_manifest = [
            "az","acr","repository","show-tags",
            "--name",f"{ACR}",
            "--repository",f"{repository}",
            "--output","tsv"
        ]
        result_tags = subprocess.run(az_cmd_show_manifest, capture_output=True, text=True)
        tags=result_tags.stdout.split()
        disct_repo_tags.setdefault(repository, []).extend(tags)
        

    with open(OUTPUT_FILE, "w") as f:
        f.write(json.dumps(disct_repo_tags, indent=2))

    return disct_repo_tags

def compareImages():

    path_kql='images/from-kql.json'
    path_k8s='images/from-k8s.json'
    path_acr='images/all-acr.json'

    with open(path_kql, 'r') as kql:
        kql_images=json.load(kql)

    with open(path_k8s, 'r') as k8s:
        k8s_images=json.load(k8s)

    with open(path_acr, 'r') as acr:
        acr_images=json.load(acr)
        
    acr_images_iterator = list(acr_images.items())
    used_images = {}

    print(acr_images_iterator)

    for repository, tags in acr_images_iterator:
        print(repository)
        if repository in kql_images:
            unused=list(set(tags) - set(kql_images[repository]))
            acr_images[repository] = unused
            used_images.setdefault(repository, []).extend(kql_images[repository])
        if repository in k8s_images:
            unused=list(set(unused) - set(k8s_images[repository]))
            acr_images[repository] = unused
            used_images.setdefault(repository, []).extend(k8s_images[repository])
        used_images[repository] = list(set(used_images.get(repository, [])))
        if unused == []:
            del acr_images[repository]
        print(used_images[repository])

    
    OUTPUT_DIR = "images"
    OUTPUT_FILE = os.path.join(OUTPUT_DIR, "unused-images.json")
    OUTPUT_FILE_2 = os.path.join(OUTPUT_DIR, "used-images.json")

    with open(OUTPUT_FILE, "w") as f:
        f.write(json.dumps(acr_images, indent=2))
    with open(OUTPUT_FILE_2, "w") as f:
        f.write(json.dumps(used_images, indent=2))

async def deleteUnusedTags():

    with open('images/unused-images.json', 'r') as file:
        unused = json.load(file)

    tasks = []

    for repository, tags in unused.items():
        for tag in tags:

            az_command=['az','acr','repository','untag','--name',f'{ACR}','--image',f'{repository}:{tag}']
            if repository == 'backstage':
                tasks.append(executeAzureCommandCLI(az_command, repository, tag))
                print(az_command)
    if tasks:
        await asyncio.gather(*tasks)

async def executeAzureCommandCLI(command, repository, tags):

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    
    stdout, stderr = await process.communicate()

    if process.returncode == 0:
        print(f"Tags deletadas do repository {repository}: {tags}")
    else:
        print(f"Erro ao deletar as tags do {repository} ({tags}): {stderr.decode()}")

asyncio.run(deleteUnusedTags())
# compareImages()
# getAllImagesFromACR()
# getImageFromKubernetesManifests()
# getImagesFromLogAnalytics()

