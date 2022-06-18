import json
from copy import deepcopy
from ..executor import Executor
from ..io import InputArtifact
from ..utils import upload_s3, randstr
from ..common import S3Artifact
from ..workflow import config
from argo.workflows.client import (
    V1Volume,
    V1VolumeMount,
    V1HostPathVolumeSource
)

class DispatcherExecutor(Executor):
    """
    Dispatcher executor

    Args:
        host: remote host
        queue_name: queue name
        port: SSH port
        username: username
        private_key_file: private key file for SSH
        image: image for dispatcher
        command: command for dispatcher
        remote_command: command for running the script remotely
        map_tmp_dir: map /tmp to ./tmp
        machine_dict: machine config for dispatcher
        resources_dict: resources config for dispatcher
        task_dict: task config for dispatcher
    """
    def __init__(self, host, queue_name, port=22, username="root", private_key_file=None, image="dptechnology/dpdispatcher", command="python", remote_command=None,
            map_tmp_dir=True, machine_dict=None, resources_dict=None, task_dict=None):
        self.host = host
        self.queue_name = queue_name
        self.port = port
        self.username = username
        self.private_key_file = private_key_file
        self.image = image
        if not isinstance(command, list):
            command = [command]
        self.command = command
        self.remote_command = remote_command
        self.map_tmp_dir = map_tmp_dir

        self.machine_dict = {
            "batch_type": "Slurm",
            "context_type": "SSHContext",
            "local_root" : "/",
            "remote_root": "/home/%s/dflow/workflows" % self.username,
            "remote_profile":{
                "hostname": self.host,
                "username": self.username,
                "port": self.port,
                "timeout": 10
            }
        }
        if machine_dict is not None:
            self.machine_dict.update(machine_dict)

        # set env to prevent dispatcher from considering different tasks as one
        self.resources_dict = {
            "number_node": 1,
            "cpu_per_node": 1,
            "gpu_per_node": 1,
            "queue_name": self.queue_name,
            "group_size": 5,
            "envs": {
                "DFLOW_WORKFLOW": "{{workflow.name}}",
                "DFLOW_POD": "{{pod.name}}"
            }
        }
        if resources_dict is not None:
            self.resources_dict.update(resources_dict)

        self.task_dict = {
            "task_work_path": "./",
            "outlog": "log",
            "errlog": "err"
        }
        if task_dict is not None:
            self.task_dict.update(task_dict)

    def render(self, template):
        new_template = deepcopy(template)
        new_template.name += "-" + randstr()
        new_template.image = self.image
        new_template.command = self.command

        if self.remote_command is None:
            self.remote_command = template.command
        map_cmd = "sed -i \\\"s#/tmp#$(pwd)/tmp#g\\\" script && " if self.map_tmp_dir else ""
        self.task_dict["command"] = "%s %s script" % (map_cmd, "".join(self.remote_command))
        self.task_dict["forward_files"] = ["script"]
        for art in template.inputs.artifacts.values():
            self.task_dict["forward_files"].append(art.path)
        for par in template.inputs.parameters.values():
            if par.save_as_artifact:
                self.task_dict["forward_files"].append(par.path)
        self.task_dict["backward_files"] = []
        for art in template.outputs.artifacts.values():
            self.task_dict["backward_files"].append("./" + art.path)
        for par in template.outputs.parameters.values():
            if par.save_as_artifact:
                self.task_dict["backward_files"].append("./" + par.path)
            else:
                self.task_dict["backward_files"].append("./" + par.value_from_path)

        new_template.script = "import os\n"
        new_template.script += "os.chdir('/')\n"
        new_template.script += "with open('script', 'w') as f:\n"
        new_template.script += "    f.write('''\n"
        new_template.script += template.script
        new_template.script += "''')\n"

        new_template.script += "import json\n"
        new_template.script += "from dpdispatcher import Machine, Resources, Task, Submission\n"
        new_template.script += "machine = Machine.load_from_dict(json.loads('%s'))\n" % json.dumps(self.machine_dict)
        new_template.script += "resources = Resources.load_from_dict(json.loads('%s'))\n" % json.dumps(self.resources_dict)
        new_template.script += "task = Task.load_from_dict(json.loads('%s'))\n" % json.dumps(self.task_dict)
        new_template.script += "submission = Submission(work_base='.', machine=machine, resources=resources, task_list=[task])\n"
        new_template.script += "submission.run_submission()\n"

        if self.private_key_file is not None:
            key = upload_s3(self.private_key_file)
            private_key_artifact = S3Artifact(key=key)
            new_template.inputs.artifacts["dflow_private_key"] = InputArtifact(path="/root/.ssh/id_rsa", source=private_key_artifact)
        else:
            new_template.volumes.append(V1Volume(name="dflow-private-key", host_path=V1HostPathVolumeSource(path=config["private_key_host_path"])))
            new_template.mounts.append(V1VolumeMount(name="dflow-private-key", mount_path="/root/.ssh/id_rsa"))
        return new_template