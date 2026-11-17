import json
import subprocess
import sys

import pytest

from app.core.paths import ROOT_DIR

IMPORT_SCRIPT = """
import json
import sys

__import__(sys.argv[1])
prefixes = ("chromadb", "numpy", "rank_bm25")
print(json.dumps({
    prefix: any(
        name == prefix or name.startswith(prefix + ".")
        for name in sys.modules
    )
    for prefix in prefixes
}))
"""


@pytest.mark.parametrize(
    "module_name",
    [
        "app.providers.vector",
        "app.core.retrieval.sparse",
        "main",
        "app.workers.message_platform",
        "app.workers.background_task",
        "app.workers.general",
        "app.workers.memory",
        "app.workers.session_reply",
    ],
)
def test_entrypoint_import_does_not_preload_heavy_vector_dependencies(module_name):
    result = subprocess.run(
        [sys.executable, "-c", IMPORT_SCRIPT, module_name],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    loaded_dependencies = json.loads(result.stdout)

    assert loaded_dependencies == {
        "chromadb": False,
        "numpy": False,
        "rank_bm25": False,
    }
