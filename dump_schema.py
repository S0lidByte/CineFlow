import sys
from pathlib import Path

sys.path.append(str(Path("src").resolve()))

import json

from program.settings.models import FilesystemModel

print(json.dumps(FilesystemModel.model_json_schema(), indent=2))
