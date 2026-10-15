"""Validate the deployment template against Zeabur's official schemas."""
import json
from urllib.request import urlopen
import yaml
from jsonschema import Draft7Validator
from referencing import Registry, Resource

BASE = 'https://raw.githubusercontent.com/zeabur/zeabur/main/schema/'
schemas = {}
for filename in ('template.json', 'prebuilt.json'):
    with urlopen(BASE + filename, timeout=30) as response:
        schemas[filename] = json.load(response)
registry = Registry().with_resources((schema['$id'], Resource.from_contents(schema)) for schema in schemas.values())
Draft7Validator(schemas['template.json'], registry=registry).validate(yaml.safe_load(open('zeabur-template.yaml')))
print('Zeabur deployment template: valid against official template and service schemas.')
