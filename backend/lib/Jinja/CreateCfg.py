"""
Jinja2 config file generator.
Uses ast.literal_eval() to parse the single-quoted Python dict literal produced
by save_data_to_database's tempStr builder. ast.literal_eval() is safe (only
handles Python literals, no code execution) and correctly handles single-quoted
keys/values that json.loads() rejects.
Also emits classic Nagios .cfg alongside LinkedEye YAML so pynag
(gethostservicedata) can read hosts written by Ubuntu/YAML templates.
"""

import ast
import os

import yaml
from jinja2 import Template


def yaml_monitor_to_nagios_cfg(yaml_path, cfg_path=None):
    """Convert a LinkedEye monitor YAML object list into classic Nagios .cfg."""
    if not yaml_path.endswith(".yaml") and not yaml_path.endswith(".yml"):
        return None
    cfg_path = cfg_path or (yaml_path.rsplit(".", 1)[0] + ".cfg")
    with open(yaml_path) as f:
        docs = yaml.safe_load(f) or []
    if not isinstance(docs, list):
        return None

    def emit_block(obj_type, data):
        lines = ["define %s {" % obj_type]
        if "use" in data:
            lines.append("\tuse\t%s" % data["use"])
        for k, v in data.items():
            if k == "use":
                continue
            if v is None:
                val = ""
            elif isinstance(v, (dict, list)):
                val = str(v).replace("'", '"')
            else:
                val = str(v)
            lines.append("\t%s\t%s" % (k, val))
        lines.append("}")
        lines.append("")
        return "\n".join(lines)

    parts = []
    for item in docs:
        if not isinstance(item, dict):
            continue
        if "service_description" in item:
            parts.append(emit_block("service", item))
        elif "host_name" in item or "address" in item:
            parts.append(emit_block("host", item))
    with open(cfg_path, "w") as f:
        f.write("\n".join(parts))
    return cfg_path


def ensure_monitor_cfgs(monitor_dir="monitor"):
    """Ensure every *_linkedeye_*.yaml under monitor/ has a matching .cfg."""
    if not os.path.isdir(monitor_dir):
        return []
    written = []
    for name in os.listdir(monitor_dir):
        if not name.endswith(".yaml"):
            continue
        if "_linkedeye_" not in name:
            continue
        yaml_path = os.path.join(monitor_dir, name)
        cfg_path = yaml_path.rsplit(".", 1)[0] + ".cfg"
        # Always refresh cfg from yaml so edits stay in sync
        out = yaml_monitor_to_nagios_cfg(yaml_path, cfg_path)
        if out:
            written.append(out)
    return written


class CreateCFG:
    def __init__(self, file_name, file_content, cfg_file_name, template_loc, cfg_loc):
        self.file_name = file_name
        self.file_content = file_content
        self.cfg_file_name = cfg_file_name
        self.template_loc = template_loc
        self.cfg_loc = cfg_loc

    def createCFGFile(self):
        self.temp_file_content = "{" + self.file_content + "}"
        self.temp_file_content_dic = ast.literal_eval(self.temp_file_content)
        with open(self.template_loc) as f:
            template = Template(f.read())
        out_path = self.cfg_loc + str(self.cfg_file_name)
        template.stream(self.temp_file_content_dic).dump(out_path)
        # Ubuntu/YAML templates write .yaml; pynag only reads classic .cfg
        if out_path.endswith(".yaml") or out_path.endswith(".yml"):
            yaml_monitor_to_nagios_cfg(out_path)
        return "Success"
