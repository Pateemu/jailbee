"""Every `litellm:` YAML example in docs/litellm.md loads through the real validation."""

import re
from pathlib import Path

import pytest
import yaml

from jailbee.config.local_layer import local_litellm_overlay, repo_litellm_view
from jailbee.global_config import GlobalConfig

_DOC = Path(__file__).parent.parent / "docs" / "litellm.md"
_BLOCKS = [
    m.group(1)
    for m in re.finditer(r"```yaml\n(.*?)```", _DOC.read_text(), re.S)
    if "litellm" in m.group(1)
]


def test_the_doc_still_has_litellm_examples():
    assert len(_BLOCKS) >= 5


@pytest.mark.parametrize("block", _BLOCKS)
def test_each_litellm_example_is_valid(block: str):
    raw = yaml.safe_load(block)
    if "repos/" in block:  # a repo's host-local file, merged over the host block
        host = GlobalConfig.model_validate({"litellm": {"enabled": True}}).litellm
        overlay = local_litellm_overlay(raw, "example.yaml")
        repo_litellm_view(host, "myrepo", overlay, "example.yaml").config.effective_routes()
    else:
        GlobalConfig.model_validate({**raw, "litellm": {"enabled": True, **raw["litellm"]}})
