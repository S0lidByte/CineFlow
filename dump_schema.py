import sys
from pathlib import Path

sys.path.append(str(Path("src").resolve()))

from program.settings.models import FilesystemModel
import json

print(json.dumps(FilesystemModel.model_json_schema(), indent=2))
