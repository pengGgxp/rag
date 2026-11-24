"""Local file executor for isolated regressions and the browser fixture; no cloud IO."""
from app.cloud_computer.cloud_file_operation import perform
from test_cloud_computer import FakeCloud


class LocalFileCloud(FakeCloud):
    def __init__(self, root, storage, prefix):
        super().__init__()
        self.root, self.storage, self.prefix = root, storage, prefix
        self.root.mkdir(parents=True, exist_ok=True)
        self.results = {}
        self.outputs = {}

    def start_file(self, sandbox, identifier, operation):
        self.starts += 1
        for key, content in self.storage.objects.items():
            if key.startswith(self.prefix):
                target = self.root / key[len(self.prefix):]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
        self.results[str(identifier)] = {"value": perform(operation, self.root)}
        for target in self.root.rglob("*"):
            if target.is_file():
                self.storage.objects[self.prefix + target.relative_to(self.root).as_posix()] = target.read_bytes()
        if operation["name"] == "delete_file":
            self.storage.objects.pop(self.prefix + operation["path"], None)
        return 42

    def file_result(self, sandbox, identifier):
        return self.results[str(identifier)]
