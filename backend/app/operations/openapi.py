from fastapi import FastAPI

_SCHEMA_NAMES = {
    "app__cloud_computer__runtime__SettingsInput": "app__cloud_computer__SettingsInput",
    "app__files__settings__TestInput": "app__storage_settings__TestInput",
    "app__model_catalog__thinking_admin__TestInput": "app__thinking_admin__TestInput",
    "app__site__settings__SettingsInput": "app__site_settings__SettingsInput",
    "app__skills__uploads__UploadInput": "app__skill_uploads__UploadInput",
}
_SCHEMA_REFS = {
    f"#/components/schemas/{source}": f"#/components/schemas/{target}"
    for source, target in _SCHEMA_NAMES.items()
}


def _rename_refs(node):
    if isinstance(node, dict):
        if "$ref" in node:
            node["$ref"] = _SCHEMA_REFS.get(node["$ref"], node["$ref"])
        for value in node.values():
            _rename_refs(value)
    elif isinstance(node, list):
        for value in node:
            _rename_refs(value)


def keep_schema_names(app: FastAPI):
    """保留目录调整前发布的同名请求模型组件标识。"""
    generate_openapi = app.openapi

    def openapi():
        if app.openapi_schema is None:
            schema = generate_openapi()
            components = schema["components"]["schemas"]
            for source, target in _SCHEMA_NAMES.items():
                if source in components:
                    components[target] = components.pop(source)
            _rename_refs(schema)
        return app.openapi_schema

    app.openapi = openapi
