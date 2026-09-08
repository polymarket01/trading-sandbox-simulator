"""Discover trusted local packages once; missing or invalid strategies never fall back."""
from dataclasses import dataclass
from functools import lru_cache
from importlib import import_module
from pathlib import Path
import json
import re

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / 'maker_strategies'
INTERNAL_MAKER_BINDING = 'CONTRACT_LADDER'  # Historical server-owned capability, not an algorithm.

@dataclass(frozen=True)
class MakerPlugin:
    manifest: dict
    directory: Path

    @property
    def key(self):
        return self.manifest['strategy_key']

    def call(self, entry, *args, **kwargs):
        module, name = self.manifest['entrypoints'][entry].split(':')
        callback = getattr(import_module(module), name)
        return callback(*args, **kwargs)

    def worker(self):
        module, name = self.manifest['entrypoints']['worker'].split(':')
        return getattr(import_module(module), name)


def discover_plugins(root=PLUGIN_ROOT):
    result = {}
    for path in sorted(Path(root).glob('*/strategy.json')):
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError('策略包不允许符号链接')
        manifest = json.loads(path.read_text(encoding='utf-8'))
        key = manifest.get('strategy_key', '')
        if manifest.get('api_version') != 1 or not re.fullmatch(r'[A-Z][A-Z0-9_]{1,63}', key):
            raise ValueError(f'无效策略契约：{path.parent.name}')
        if key in result:
            raise ValueError(f'重复策略：{key}')
        products = manifest.get('products')
        if not isinstance(products, list) or not products or set(products) - {'SPOT', 'PERP'}:
            raise ValueError(f'无效策略产品：{key}')
        if type(manifest.get('account_slots')) is not int or not 1 <= manifest['account_slots'] <= 8:
            raise ValueError(f'无效账户槽位：{key}')
        if manifest.get('executor') not in {'managed', 'legacy'}:
            raise ValueError(f'无效执行器：{key}')
        if manifest['executor'] == 'managed':
            for entry in ('default', 'validate', 'worker', 'preview'):
                reference = manifest.get('entrypoints', {}).get(entry, '')
                prefix = f'app.maker_strategies.{path.parent.name}.'
                if not reference.startswith(prefix) or not re.fullmatch(r'[\w.]+:[A-Za-z_]\w*', reference):
                    raise ValueError(f'策略入口必须属于自己的包：{key}/{entry}')
                module = reference.split(':')[0].split('.')[3:]
                if not path.parent.joinpath(*module).with_suffix('.py').is_file():
                    raise ValueError(f'策略入口文件缺失：{key}/{entry}')
        result[key] = MakerPlugin(manifest, path.parent)
    return result

@lru_cache(maxsize=1)
def installed_plugins():
    return discover_plugins()

def get_plugin(key):
    try:
        return installed_plugins()[str(key).upper()]
    except KeyError:
        raise ValueError(f'策略未安装：{key}') from None

def strategy_choices(product):
    return [key for key, plugin in installed_plugins().items() if product in plugin.manifest['products']]

INTERNAL_MAKER_STRATEGIES = tuple(key for key, plugin in installed_plugins().items() if plugin.manifest['executor'] == 'managed')

def internal_default(key, symbol):
    return get_plugin(key).call('default', symbol)

def internal_validate(key, config, metadata, capabilities):
    return get_plugin(key).call('validate', config, metadata, capabilities)

def internal_worker(key):
    return get_plugin(key).worker()
