import json
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from zipfile import BadZipFile


Sku = tuple[tuple[str, str], ...]
SOLD_OUT = '缺货'
OWN_SHOP_NAME = '大飞体育用品数码店'


@dataclass(frozen=True)
class Quote:
    spec: Sku
    price: Decimal | None
    observed_at: str
    status: str = ''
    reason: str = ''


@dataclass(frozen=True)
class Product:
    goods_id: str
    shop_name: str
    title: str
    quotes: tuple[Quote, ...] = ()
    issue: str = ''


@dataclass
class Pair:
    own_id: str
    competitor_id: str
    sku_links: dict[Sku, Sku] = field(default_factory=dict)


_FULLWIDTH_ASCII = {code + 0xFEE0: code for code in range(0x21, 0x7F)}
_FIELDS = ('店名', '标题', '商品ID', '规格', '券前价')
_COLUMNS = tuple(side + name for side in ('自家', '对手') for name in _FIELDS) + (
    '差价', '状态', '原因', '未配对店名', '未配对标题', '未配对商品ID',
)
_INVENTORY_COLUMNS = ('店铺名', '商品名称', '关键名称', '券前价', '状态', '原因')
_REPORT_COLUMNS = {'规格明细': _INVENTORY_COLUMNS, '差价': _COLUMNS}
_SCAN_COMPARISON_COLUMNS = (
    '关键名称', '自家店铺', '自家券前价', '竞品店铺', '竞品券前价', '差价', '自家筒数', '竞品筒数', '状态',
)
_BRANDS = {
    'Victor': re.compile(r'胜利|威克多|(?<![a-z])victor(?=no\.?\s*[0-9]|[^a-z]|$)', re.I),
    '李宁': re.compile(r'李宁|(?<![a-z])li[\s-]?ning(?=g[0-9]|[^a-z]|$)', re.I),
    '亚狮龙': re.compile(r'亚狮龙|(?<![a-z])rsl(?=d[0-9]|[^a-z]|$)', re.I),
    '尤尼克斯': re.compile(r'尤尼克斯|(?<![a-z])yonex(?=as[ -]?[0-9]|[^a-z]|$)', re.I),
    '华美': re.compile(r'华美'),
}
_MODEL = re.compile(
    r'(?<![a-z0-9])(?:no\.?\s*[0-9]+号|gt[0-9]{3}|g[0-9]{3}s?|as[ -]?[0-9]{2}|d[0-9]{1,2}|[0-9]{3}s)(?![a-z0-9])',
    re.I,
)
_SPEED = re.compile(r'(?<![a-z0-9])(?<![0-9][/、~至-])([78][0-9])\s*(?:su?(?![a-z])|速)', re.I)
_TUBES = re.compile(r'(?<![\d.一二两三四五六七八九十])([1-9]\d*|[单一二两三四五六七八九十])\s*[筒桶]')
_CN_COUNTS = dict(zip('一二三四五六七八九十', range(1, 11))) | {'单': 1, '两': 2}


def shop_role(shop_name: str) -> str:
    name = shop_name.strip()
    if not name:
        return '店铺未确认'
    return '自家' if name == OWN_SHOP_NAME else '竞品'


def sku_key(spec: Sku) -> Sku:
    def normalize(value: str) -> str:
        return ''.join(value.translate(_FULLWIDTH_ASCII).split())

    return tuple(sorted((normalize(name), normalize(value)) for name, value in spec))


def _valid_key(key: Sku) -> bool:
    return all(name and value for name, value in key) and len({name for name, _ in key}) == len(key)


def _read_spec(raw: object) -> Sku:
    if not isinstance(raw, (list, tuple)) or any(
        not isinstance(item, (list, tuple)) or len(item) != 2
        or not all(isinstance(value, str) for value in item)
        for item in raw
    ):
        raise ValueError('规格必须由规格项和选项组成')
    key = sku_key(tuple((name, value) for name, value in raw))
    if not _valid_key(key):
        raise ValueError('规格项或选项为空，或规格项名称重复')
    return key


def _parse_pairs(raw: object) -> list[Pair]:
    if not isinstance(raw, dict) or not isinstance(raw.get('pairs'), list):
        raise ValueError('缺少商品配对列表 pairs')
    pairs = []
    product_links = set()
    for index, item in enumerate(raw['pairs'], 1):
        if not isinstance(item, dict):
            raise ValueError(f'第 {index} 组商品配对格式无效')
        own_id, competitor_id = item.get('own_id'), item.get('competitor_id')
        if any(not isinstance(value, str) or not value.strip() for value in (own_id, competitor_id)):
            raise ValueError(f'第 {index} 组配对缺少商品 ID')
        product_link = frozenset((own_id, competitor_id))
        if len(product_link) != 2 or product_link in product_links:
            raise ValueError(f'第 {index} 组商品配对重复，或自家与对手商品 ID 相同')
        product_links.add(product_link)
        if not isinstance(item.get('sku_links'), list):
            raise ValueError(f'第 {index} 组配对缺少规格配对列表 sku_links')
        sku_links = {}
        competitor_keys = set()
        for link in item['sku_links']:
            try:
                if not isinstance(link, dict):
                    raise ValueError('规格配对格式无效')
                own_key = _read_spec(link.get('own'))
                competitor_key = _read_spec(link.get('competitor'))
                if own_key in sku_links or competitor_key in competitor_keys:
                    raise ValueError('同一规格对应了多个配对记录')
            except ValueError as error:
                raise ValueError(f'第 {index} 组商品的规格配对无效：{error}') from error
            sku_links[own_key] = competitor_key
            competitor_keys.add(competitor_key)
        pairs.append(Pair(own_id, competitor_id, sku_links))
    return pairs


def read_pairs(path: Path) -> list[Pair]:
    try:
        return _parse_pairs(json.loads(path.read_text(encoding='utf-8')))
    except FileNotFoundError:
        return []
    except ValueError as error:
        raise ValueError(f'配对文件 {path} 无效：{error}') from error


@contextmanager
def _atomic_output(path: Path, mode: str, encoding: str | None = None, *, overwrite: bool = True):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.NamedTemporaryFile(
        mode=mode, encoding=encoding, dir=path.parent, delete=False,
    )
    temporary_path = Path(temporary.name)
    try:
        with temporary:
            yield temporary
            temporary.flush()
            os.fsync(temporary.fileno())
        if overwrite:
            temporary_path.replace(path)
        else:
            os.link(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def save_pairs(path: Path, pairs: list[Pair]) -> None:
    payload = {'pairs': [
        {
            'own_id': pair.own_id,
            'competitor_id': pair.competitor_id,
            'sku_links': [
                {'own': own, 'competitor': competitor}
                for own, competitor in pair.sku_links.items()
            ],
        }
        for pair in pairs
    ]}
    _parse_pairs(payload)
    with _atomic_output(path, 'w', 'utf-8') as output:
        json.dump(payload, output, ensure_ascii=False, indent=2)
        output.write('\n')


def _spec_text(spec: Sku) -> str:
    return '；'.join(f'{name}：{value}' for name, value in spec) or '无多种规格'


def _model_names(text: str, brand: str):
    if brand == 'Victor':
        text = re.sub(r'比赛\s*(\d{1,2})号?(?!\d)', r'NO.\1号', text)
    for pattern in _BRANDS.values():
        text = pattern.sub(' ', text)
    for match in _MODEL.findall(text):
        model = ''.join(match.split()).upper()
        if model.startswith('NO'):
            yield 'NO.' + model[2:].lstrip('.')
        elif model.startswith('AS'):
            yield 'AS-' + model[2:].lstrip('-')
        else:
            yield model.lower() if model[0].isdigit() else model


def name_parts(title: str, spec: Sku) -> tuple[str, str, str]:
    selected = ' '.join(value for _, value in spec).translate(_FULLWIDTH_ASCII)
    title = title.translate(_FULLWIDTH_ASCII)

    def unique_match(find):
        for text in (selected, title):
            matches = set(find(text))
            if matches:
                return next(iter(matches)) if len(matches) == 1 else ''
        return ''

    brand = unique_match(lambda text: (name for name, pattern in _BRANDS.items() if pattern.search(text)))
    model = unique_match(lambda text: _model_names(text, brand))
    speed = unique_match(_SPEED.findall)
    return brand, model, f'{speed}速' if speed else ''


def _short_name(title: str, spec: Sku) -> str:
    parts = name_parts(title, spec)
    if not any(parts[1:]):
        return _spec_text(spec)
    return ' '.join(part for part in parts if part) or _spec_text(spec)


def _price_text(value: Decimal | None) -> str:
    return format(value, 'f') if value is not None and value.is_finite() else ''


def _quote_issue(quote: Quote) -> str:
    if quote.status not in ('', 'available', 'ok', '可售'):
        return f'{quote.status}（{quote.reason}）' if quote.reason else quote.status
    if not _price_text(quote.price):
        return quote.reason or '未读到明确券前价'
    return ''


def _quotes_by_key(product: Product) -> dict[Sku, list[Quote]]:
    groups = {}
    for quote in product.quotes:
        groups.setdefault(sku_key(quote.spec), []).append(quote)
    return groups


def _sold_out_group(quotes: list[Quote]) -> bool:
    return bool(quotes) and all(quote.status == SOLD_OUT for quote in quotes)


def comparison_rows(products: list[Product], pairs: list[Pair], *, scan_complete: bool) -> list[dict[str, str]]:
    by_id = {product.goods_id: product for product in products}
    rows = []
    paired_ids = set()

    for pair in pairs:
        paired_ids.update((pair.own_id, pair.competitor_id))
        own = by_id.get(pair.own_id, Product(pair.own_id, '', ''))
        competitor = by_id.get(pair.competitor_id, Product(pair.competitor_id, '', ''))

        def add(own_quote=None, competitor_quote=None, *, status, reason='', difference=None):
            row = dict.fromkeys(_COLUMNS, '')
            for side, product, quote in (('自家', own, own_quote), ('对手', competitor, competitor_quote)):
                values = (
                    product.shop_name, product.title, product.goods_id,
                    _spec_text(quote.spec) if quote else '',
                    _price_text(quote.price) if quote else '',
                )
                row.update((side + name, value) for name, value in zip(_FIELDS, values))
            row.update({'差价': _price_text(difference), '状态': status, '原因': reason})
            rows.append((difference, row))

        def report_unmatched(own_quotes, competitor_quotes, *, status, reason):
            own_quotes = [quote for quote in own_quotes if quote.status != SOLD_OUT]
            competitor_quotes = [quote for quote in competitor_quotes if quote.status != SOLD_OUT]
            if not own_quotes and not competitor_quotes:
                add(status=status, reason=reason)
            for quote in own_quotes:
                add(own_quote=quote, status=status, reason=reason)
            for quote in competitor_quotes:
                add(competitor_quote=quote, status=status, reason=reason)

        def report_match(own_quotes, competitor_quotes):
            own_quote, competitor_quote = own_quotes[0], competitor_quotes[0]
            issues = [
                f'{side}：{issue}'
                for side, quote in (('自家', own_quote), ('对手', competitor_quote))
                if (issue := _quote_issue(quote))
            ]
            if issues or own_quote.price is None or competitor_quote.price is None:
                add(own_quote, competitor_quote, status='价格不可比较', reason='；'.join(issues))
                return
            add(own_quote, competitor_quote, status='已比价', difference=own_quote.price - competitor_quote.price)

        missing = [
            side for side, product in (('自家', own), ('对手', competitor))
            if product.goods_id not in by_id
        ]
        if missing:
            status = '收藏中缺少商品' if scan_complete else '采集未完成'
            detail = '未在收藏中找到' if scan_complete else '尚未采到'
            report_unmatched(own.quotes, competitor.quotes, status=status, reason=f'{"、".join(missing)}商品{detail}')

        issues = [
            f'{side}：{product.issue or "未采到规格"}'
            for side, product in (('自家', own), ('对手', competitor))
            if product.goods_id in by_id and (product.issue or not product.quotes)
        ]
        if issues:
            add(status='商品采集异常', reason='；'.join(issues))
        if missing:
            continue

        own_groups = _quotes_by_key(own)
        competitor_groups = _quotes_by_key(competitor)
        for own_spec, competitor_spec in pair.sku_links.items():
            own_key, competitor_key = sku_key(own_spec), sku_key(competitor_spec)
            own_quotes = own_groups.pop(own_key, [])
            competitor_quotes = competitor_groups.pop(competitor_key, [])
            if _sold_out_group(own_quotes) or _sold_out_group(competitor_quotes):
                continue
            if not own_quotes or not competitor_quotes:
                reason = f'自家：{_spec_text(own_spec)}；对手：{_spec_text(competitor_spec)}'
                incomplete = (not own_quotes and (own.issue or not own.quotes)) or (
                    not competitor_quotes and (competitor.issue or not competitor.quotes)
                )
                status = '手动配对规格未采到' if incomplete else '手动规格配对失效'
                report_unmatched(own_quotes, competitor_quotes, status=status, reason=reason)
            elif len(own_quotes) != 1 or len(competitor_quotes) != 1:
                report_unmatched(own_quotes, competitor_quotes, status='规格歧义', reason='手动配对的规格有多条采集记录')
            else:
                report_match(own_quotes, competitor_quotes)

        for key in dict.fromkeys((*own_groups, *competitor_groups)):
            own_quotes = own_groups.get(key, [])
            competitor_quotes = competitor_groups.get(key, [])
            if _sold_out_group(own_quotes) or _sold_out_group(competitor_quotes):
                continue
            if not _valid_key(key) or len(own_quotes) > 1 or len(competitor_quotes) > 1:
                report_unmatched(own_quotes, competitor_quotes, status='规格歧义', reason='完整规格无法唯一识别')
            elif not own_quotes or not competitor_quotes:
                report_unmatched(own_quotes, competitor_quotes, status='未匹配规格', reason='另一方未采到相同完整规格')
            else:
                report_match(own_quotes, competitor_quotes)

    for product in products:
        if product.goods_id not in paired_ids:
            row = dict.fromkeys(_COLUMNS, '')
            row.update({
                '未配对店名': product.shop_name, '未配对标题': product.title, '未配对商品ID': product.goods_id,
                '状态': '未配对商品', '原因': product.issue,
            })
            rows.append((None, row))

    rows.sort(key=lambda item: (item[0] is not None, item[0]), reverse=True)
    return [row for _, row in rows]


def write_comparison_xlsx(path: Path, rows: list[dict[str, str]]) -> None:
    _write_xlsx(path, [('差价', _COLUMNS, rows, {'自家券前价', '对手券前价', '差价'})])


def is_price_report(path: Path) -> bool:
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(path, read_only=True)
        try:
            sheet = workbook.worksheets[0]
            columns = _REPORT_COLUMNS.get(sheet.title)
            return columns is not None and next(sheet.values, ()) == columns
        finally:
            workbook.close()
    except (BadZipFile, IndexError, KeyError, OSError, SyntaxError, ValueError):
        return False


def _tube_count(title: str, spec: Sku) -> int | None:
    for text in (' '.join(value for _, value in spec), title):
        counts = {
            _CN_COUNTS[count] if count in _CN_COUNTS else int(count)
            for count in _TUBES.findall(text.translate(_FULLWIDTH_ASCII))
        }
        if counts:
            return next(iter(counts)) if len(counts) == 1 else None
    return None


def _scan_comparison_rows(products: list[Product]) -> list[dict[str, str]]:
    own = {}
    competitors = {}
    for product in products:
        if not product.shop_name.strip():
            continue
        groups = own if shop_role(product.shop_name) == '自家' else competitors
        for quote in product.quotes:
            parts = name_parts(product.title, quote.spec)
            if not all(parts) or _quote_issue(quote):
                continue
            count = _tube_count(product.title, quote.spec)
            groups.setdefault(parts, []).append((product.shop_name, quote.price, count))

    rows = []
    for parts, offers in own.items():
        for shop, price, count in offers:
            for rival, rival_price, rival_count in competitors.get(parts, ()):
                if count is not None and rival_count is not None and count != rival_count:
                    continue
                can_compare = count is not None and count == rival_count
                rows.append({
                    '关键名称': ' '.join(parts),
                    '自家店铺': shop, '自家券前价': _price_text(price),
                    '竞品店铺': rival, '竞品券前价': _price_text(rival_price),
                    '差价': _price_text(price - rival_price) if can_compare else '',
                    '自家筒数': str(count) if count is not None else '',
                    '竞品筒数': str(rival_count) if rival_count is not None else '',
                    '状态': '已比价' if can_compare else '筒数待核对',
                })
    rows.sort(key=lambda row: -Decimal(row['差价']) if row['差价'] else Decimal('Infinity'))
    return rows


def write_inventory_xlsx(path: Path, products: list[Product]) -> int:
    rows = []
    for product in products:
        identity = {'店铺名': product.shop_name, '商品名称': product.title}
        for quote in product.quotes:
            if quote.status == SOLD_OUT:
                continue
            issue = _quote_issue(quote)
            rows.append({
                **identity,
                '关键名称': _short_name(product.title, quote.spec),
                '券前价': '' if issue else _price_text(quote.price),
                '状态': '待核对' if issue else '已采集',
                '原因': issue,
            })
        if product.issue:
            rows.append({**identity, '状态': '采集异常', '原因': product.issue})
    _write_xlsx(path, [
        ('规格明细', _INVENTORY_COLUMNS, rows, {'券前价'}),
        ('同规格比价', _SCAN_COMPARISON_COLUMNS, _scan_comparison_rows(products),
         {'自家券前价', '竞品券前价', '差价', '自家筒数', '竞品筒数'}),
    ])
    return len(rows)


def _write_xlsx(path: Path, sheets: list[tuple[str, tuple[str, ...], list[dict[str, str]], set[str]]]) -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    for index, (sheet_name, columns, rows, numeric_fields) in enumerate(sheets):
        sheet = workbook.active if index == 0 else workbook.create_sheet()
        _fill_sheet(sheet, sheet_name, columns, rows, numeric_fields)
    with _atomic_output(path, 'w+b', overwrite=False) as output:
        workbook.save(output)


def _fill_sheet(sheet, sheet_name: str, columns: tuple[str, ...], rows: list[dict[str, str]],
                numeric_fields: set[str]) -> None:
    from openpyxl.styles import Font, PatternFill

    sheet.title = sheet_name
    sheet.append(columns)
    sheet.freeze_panes = 'A2'

    for row_index, row in enumerate(rows, start=2):
        for index, field in enumerate(columns, start=1):
            value = row.get(field, '')
            if not value:
                continue
            if field in numeric_fields:
                cell = sheet.cell(row_index, index, Decimal(value))
                cell.number_format = '#,##0.00'
            else:
                cell = sheet.cell(row_index, index, value)
                cell.data_type = 's'

    for cell in sheet[1]:
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill(fill_type='solid', fgColor='29475E')
        field = cell.value
        width = 42 if any(word in field for word in ('商品名称', '关键名称', '标题', '规格', '原因')) else 24
        sheet.column_dimensions[cell.column_letter].width = width
    sheet.auto_filter.ref = sheet.dimensions
