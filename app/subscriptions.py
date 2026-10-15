"""Download and combine subscription sources without mixing routing policies."""
import asyncio
import copy
import hashlib
from urllib.parse import urlsplit
import yaml
from .core import MAX_CONFIG

MAX_SOURCES = 20


def parse_urls(value):
    if isinstance(value, str):
        value = value.splitlines()
    if not isinstance(value, list) or any(not isinstance(url, str) for url in value):
        raise ValueError('订阅地址需要文本或 URL 数组')
    urls = list(dict.fromkeys(url.strip() for url in value if url.strip()))
    if len(urls) > MAX_SOURCES:
        raise ValueError(f'最多导入 {MAX_SOURCES} 个提供商 URL')
    for index, url in enumerate(urls, 1):
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username:
            raise ValueError(f'提供商 {index} 的地址必须是 HTTP(S) URL')
    return urls


def merge_subscriptions(urls, texts):
    if not urls or len(urls) != len(texts):
        raise ValueError('订阅地址与 YAML 内容不匹配')
    if sum(len(text.encode()) for text in texts) > MAX_CONFIG:
        raise ValueError('所有订阅的 YAML 总大小超过 5 MiB')
    if len(texts) == 1:
        return texts[0]  # Preserve complete single-source configurations.
    merged = {'proxies': [], 'proxy-providers': {}, 'proxy-groups': [], 'rules': ['MATCH,节点选择']}
    source_groups = []
    for index, (url, text) in enumerate(zip(urls, texts), 1):
        label = f'提供商{index}'
        try:
            source = yaml.safe_load(text)
        except yaml.YAMLError as error:
            raise ValueError(f'{label} 的 YAML 格式错误') from error
        if not isinstance(source, dict):
            raise ValueError(f'{label} 需要 Clash / Mihomo YAML')
        proxies = source.get('proxies') or []
        providers = source.get('proxy-providers') or {}
        if not isinstance(proxies, list) or not isinstance(providers, dict):
            raise ValueError(f'{label} 的 proxies 或 proxy-providers 格式错误')
        if not proxies and not providers:
            raise ValueError(f'{label} 没有可导入的节点或代理集合')
        names = {}
        for proxy in proxies:
            name = proxy.get('name') if isinstance(proxy, dict) else None
            if not isinstance(name, str) or not name.strip() or name in names:
                raise ValueError(f'{label} 含有无名称或重名节点')
            names[name] = f'[{label}] {name}'
        for proxy in proxies:
            node = copy.deepcopy(proxy)
            node['name'] = names[node['name']]
            if isinstance(node.get('dialer-proxy'), str) and node['dialer-proxy'] in names:
                node['dialer-proxy'] = names[node['dialer-proxy']]
            merged['proxies'].append(node)
        provider_names = []
        for name, value in providers.items():
            if not isinstance(name, str) or not name or not isinstance(value, dict):
                raise ValueError(f'{label} 的代理集合格式错误')
            provider = copy.deepcopy(value)
            override = provider.get('override') or {}
            if not isinstance(override, dict) or not isinstance(override.get('additional-prefix', ''), str):
                raise ValueError(f'{label} 的代理集合覆写格式错误')
            override['additional-prefix'] = f'[{label}/{name}] ' + override.get('additional-prefix', '')
            if isinstance(override.get('dialer-proxy'), str) and override['dialer-proxy'] in names:
                override['dialer-proxy'] = names[override['dialer-proxy']]
            provider['override'] = override
            if isinstance(provider.get('proxy'), str) and provider['proxy'] in names:
                provider['proxy'] = names[provider['proxy']]
            if provider.get('type') == 'http':
                digest = hashlib.sha256((url + '\0' + name).encode()).hexdigest()[:24]
                provider['path'] = f'./providers/miho-{digest}.yaml'
            key = f'[{label}] {name}'
            merged['proxy-providers'][key] = provider
            provider_names.append(key)
        group_name = f'[{label}]'
        group = {'name': group_name, 'type': 'select'}
        if names:
            group['proxies'] = list(names.values()) + ['DIRECT']
        if provider_names:
            group['use'] = provider_names
        merged['proxy-groups'].append(group)
        source_groups.append(group_name)
    merged['proxy-groups'].insert(0, {'name': '节点选择', 'type': 'select', 'proxies': source_groups + ['DIRECT']})
    if not merged['proxy-providers']:
        merged.pop('proxy-providers')
    return yaml.safe_dump(merged, allow_unicode=True, sort_keys=False)


async def load_subscriptions(urls, download):
    semaphore = asyncio.Semaphore(4)

    async def fetch(index, url):
        async with semaphore:
            try:
                return await download(url)
            except ValueError as error:
                raise ValueError(f'提供商 {index}：{error}') from error

    tasks = [asyncio.create_task(fetch(index, url)) for index, url in enumerate(urls, 1)]
    try:
        texts = await asyncio.wait_for(asyncio.gather(*tasks), 45)
    except BaseException as error:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if isinstance(error, asyncio.TimeoutError):
            raise ValueError('订阅下载超时，请稍后重试') from error
        raise
    return merge_subscriptions(urls, texts)
