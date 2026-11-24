"""从拼多多收藏列表读取店铺分组、商品规格和券前价。"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from itertools import product as combinations
import logging
from random import uniform
import re
from time import monotonic, sleep
from typing import Iterator
from xml.etree import ElementTree as ET

from pdd_compare import SOLD_OUT, Product, Quote, Sku, name_parts


log = logging.getLogger('拼多多采集')
_PACKAGE = 'com.xunmeng.pinduoduo'
_RECYCLER = 'androidx.recyclerview.widget.RecyclerView'
_TEXT_VIEW = 'android.widget.TextView'
_GROUP = 'android.view.ViewGroup'
_LINEAR = 'android.widget.LinearLayout'
_CHECKBOX = '勾选按钮，商品'
_RECOMMENDATIONS = '精选推荐'
_PRICE = re.compile(r'券前\s*[¥￥]\s*([0-9]+(?:\.[0-9]{1,2})?)')
_QUANTITY = re.compile(r'×\s*[0-9]+')
_SOLD_OUT = re.compile(r'(?:刚刚抢光|已抢光|已售罄|无货|缺货)$')
_OPTION_BADGE = re.compile(r'\s+(?:[0-9]+(?:\.[0-9]+)?%人选择|快要抢光)$')
_BOUNDS = re.compile(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]')


class CaptureError(RuntimeError):
    """App 或界面已无法按已确认的步骤继续采集。"""


class _SpecificationError(CaptureError):
    """当前商品的规格无法完整解析。"""


@dataclass(frozen=True)
class _Entry:
    kind: str
    label: str
    choice: str
    bounds: str
    capsule: ET.Element | None = None
    clipped: bool = False
    title_node: ET.Element | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return self.kind, self.label, self.choice


@dataclass(frozen=True)
class _Option:
    label: str
    node: ET.Element
    sold_out: bool
    caption: ET.Element | None = None


def connect_device(serial: str | None):
    try:
        import uiautomator2

        log.info('连接 ADB 设备 %s', serial or '（自动选择）')
        device = uiautomator2.connect(serial)
        log.info('设备已连接，序列号=%s', device.serial)
        return device
    except Exception as error:
        raise CaptureError(f'无法连接安卓设备（{type(error).__name__}），请确认 ADB 已识别设备。') from error


def _tree(device) -> ET.Element:
    log.info('读取当前界面层级')
    return ET.fromstring(device.dump_hierarchy())


def _text(node: ET.Element) -> str:
    return node.get('text', '').strip()


def _desc(node: ET.Element) -> str:
    return node.get('content-desc', '')


def _class(node: ET.Element) -> str:
    return node.get('class', '')


def _flag(node: ET.Element, name: str) -> bool:
    return node.get(name) == 'true'


def _box(node: ET.Element) -> tuple[int, int, int, int]:
    match = _BOUNDS.fullmatch(node.get('bounds', ''))
    if match is None:
        raise CaptureError('可操作的控件缺少有效坐标')
    return tuple(map(int, match.groups()))


def _pause_after_action() -> None:
    duration = uniform(0.0, 1.0)
    log.info('操作后停顿 %.2f 秒', duration)
    sleep(duration)


def _click(device, node: ET.Element) -> None:
    left, top, right, bottom = _box(node)
    if right <= left or bottom <= top:
        raise CaptureError('目标控件已滚出屏幕')
    log.info('点击控件：文字=%r 描述=%r 区域=%s', _text(node), _desc(node), node.get('bounds'))
    device.click((left + right) // 2, (top + bottom) // 2)
    _pause_after_action()


def _scroll_next(device, viewport: ET.Element) -> None:
    left, top, right, bottom = _box(viewport)
    x = left + (right - left) * 2 // 3
    high = top + (bottom - top) * 28 // 100
    low = top + (bottom - top) * 76 // 100
    log.info('收藏列表翻页：(%d,%d) → (%d,%d)', x, low, x, high)
    device.swipe(x, low, x, high, duration=0.45)
    _pause_after_action()
    sleep(0.4)


def _recommendations(node: ET.Element) -> bool:
    return any(_text(child) == _RECOMMENDATIONS or _desc(child).strip() == _RECOMMENDATIONS
               for child in node.iter('node'))


def _list(root: ET.Element, stage: str = '读取收藏列表') -> ET.Element:
    app_nodes = [node for node in root.iter('node') if node.get('package') == _PACKAGE]
    has_sheet = _sheet(root) is not None
    lists = [
        node for node in app_nodes
        if _class(node) == _RECYCLER
        and any(_desc(child).startswith(_CHECKBOX) for child in node.iter('node'))
    ]
    packages = sorted({node.get('package') for node in root.iter('node') if node.get('package')})
    log.info('%s：界面包名=%s，拼多多节点=%d，规格弹层=%s，商品列表匹配数=%d',
             stage, packages, len(app_nodes), has_sheet, len(lists))
    if not app_nodes:
        raise CaptureError(f'{stage}时未读到拼多多界面，请查看日志中的界面包名')
    if has_sheet:
        raise CaptureError(f'{stage}时规格弹层仍处于打开状态')
    if not lists:
        # 到达推荐区域时，最后一张收藏商品卡可能已经滚出屏幕。
        for node in app_nodes:
            if _class(node) == _RECYCLER and _recommendations(node):
                return node
    if len(lists) != 1:
        raise CaptureError(f'{stage}时收藏商品列表匹配到 {len(lists)} 个')
    return lists[0]


def _store(item: ET.Element) -> str:
    for header in item.iter('node'):
        if _class(header) != _GROUP or not _flag(header, 'clickable'):
            continue
        if not any(_text(child) == '\ue617' for child in header.iter('node')):
            continue
        for child in header:
            layout = [_class(node) for node in child]
            if (layout[:2] == ['android.widget.ImageView', _TEXT_VIEW]
                    and all(kind == 'android.widget.ImageView' for kind in layout[2:])):
                return _text(child[1])
    return ''


def _favorite(item: ET.Element, viewport: ET.Element) -> _Entry | None:
    for row in item.iter('node'):
        if _class(row) == _GROUP and any(_desc(child).startswith(_CHECKBOX) for child in row):
            break
    else:
        return None
    _, list_top, _, list_bottom = _box(viewport)
    _, row_top, _, row_bottom = _box(row)
    clipped = row_top <= list_top or row_bottom >= list_bottom
    if len(row) < 3:
        return _Entry('product', '', '', row.get('bounds', ''), clipped=clipped)
    content = row[2]
    first = list(content[0].iter('node')) if len(content) else []
    title_node = next((node for node in first if _class(node) == _TEXT_VIEW and _text(node)), None)
    title = _text(title_node) if title_node is not None else ''
    parents = {child: parent for parent in content.iter('node') for child in parent}
    for node in content.iter('node'):
        text = _text(node)
        if _class(node) != _TEXT_VIEW or (text != '请选择款式' and not _QUANTITY.fullmatch(text)):
            continue
        capsule = parents.get(node)
        while capsule is not None and not _flag(capsule, 'clickable'):
            capsule = parents.get(capsule)
        if capsule is not None and not _desc(capsule):
            choice = ' '.join(_text(child) for child in capsule.iter('node') if _text(child))
            return _Entry('product', title, choice, row.get('bounds', ''), capsule, clipped, title_node)
    return _Entry('product', title, '', row.get('bounds', ''), clipped=clipped)


def _page(device, stage: str = '读取收藏列表') -> tuple[ET.Element, list[_Entry]]:
    viewport = _list(_tree(device), stage)
    entries = []
    for item in viewport:
        favorite = _favorite(item, viewport)
        if favorite is not None:
            entries.append(favorite)
        elif name := _store(item):
            entries.append(_Entry('shop', name, '', item.get('bounds', '')))
        elif _recommendations(item):
            entries.append(_Entry('end', _RECOMMENDATIONS, '', item.get('bounds', '')))
            break
    log.info('解析收藏列表：直属节点=%d，店铺=%d，商品=%d，有规格入口=%d', len(viewport),
             sum(entry.kind == 'shop' for entry in entries), sum(entry.kind == 'product' for entry in entries),
             sum(entry.capsule is not None for entry in entries))
    return viewport, entries


def _signature(entries: list[_Entry]) -> tuple:
    return tuple((entry.key, entry.bounds) for entry in entries)


def _startup_entries(root: ET.Element, label: str) -> list[ET.Element]:
    return [
        node for node in root.iter('node')
        if node.get('package') == _PACKAGE
        and (_text(node) == label or _desc(node).strip() == label)
        and _flag(node, 'clickable') and _flag(node, 'enabled')
    ]


def _click_startup_entry(device, label: str, *, stable_for: float = 0.0) -> None:
    deadline = monotonic() + 15
    previous_bounds = None
    stable_since = None
    if stable_for:
        log.info('等待「%s」入口稳定 %.1f 秒', label, stable_for)
    while True:
        matches = _startup_entries(_tree(device), label)
        now = monotonic()
        if len(matches) == 1:
            bounds = matches[0].get('bounds')
            if stable_since is None or bounds != previous_bounds:
                stable_since = now
                previous_bounds = bounds
            if now - stable_since >= stable_for:
                _click(device, matches[0])
                return
        else:
            stable_since = None
        if now >= deadline:
            raise CaptureError(f'等待「{label}」入口超时，匹配到 {len(matches)} 个可点击控件')
        sleep(0.25)


def _wait_favorites_page(device, stage: str, *, settle: bool = False,
                         timeout: float = 15) -> tuple[ET.Element, list[_Entry]]:
    deadline = monotonic() + timeout
    previous = None
    while True:
        try:
            viewport, entries = _page(device, stage)
        except CaptureError as error:
            previous = None
            reason = str(error)
        else:
            signature = (viewport.get('bounds'), _signature(entries))
            if not settle or signature == previous:
                return viewport, entries
            previous = signature
            reason = '收藏列表尚未稳定'
        if monotonic() >= deadline:
            raise CaptureError(f'{stage}超时：{reason}')
        sleep(0.25)


def _start_favorites(device) -> tuple[ET.Element, list[_Entry]]:
    log.info('重新打开拼多多')
    device.app_start(_PACKAGE, stop=True, wait=True)
    _click_startup_entry(device, '个人中心')
    _click_startup_entry(device, '商品收藏', stable_for=1.0)
    try:
        viewport, _ = _wait_favorites_page(device, '进入收藏列表', timeout=3)
    except CaptureError:
        root = _tree(device)
        entries = _startup_entries(root, '商品收藏')
        if len(entries) == 1 and any(
            node.get('package') == _PACKAGE and _text(node) == '我的订单' for node in root.iter('node')
        ):
            log.info('点击后仍停在个人中心，重新点击「商品收藏」一次')
            _click(device, entries[0])
        viewport, _ = _wait_favorites_page(device, '等待收藏列表出现')
    left, top, right, bottom = _box(viewport)
    x = (left + right) // 2
    height = bottom - top
    log.info('下拉刷新收藏列表一次')
    device.swipe(x, top + height * 20 // 100, x, top + height * 78 // 100, duration=0.6)
    _pause_after_action()
    sleep(0.4)
    page = _wait_favorites_page(device, '等待刷新后收藏列表稳定', settle=True)
    log.info('收藏列表已稳定，开始采集')
    return page


def _sheet(root: ET.Element) -> ET.Element | None:
    parents = {child: parent for parent in root.iter() for child in parent}
    for node in root.iter('node'):
        if node.get('package') != _PACKAGE or _desc(node) != '关闭':
            continue
        for parent in _ancestors(node, parents):
            if _class(parent) == 'android.widget.RelativeLayout' and any(
                _text(child) in ('确定', '再来一单') for child in parent.iter('node')
            ):
                return parent
    return None


def _ancestors(node: ET.Element, parents: dict[ET.Element, ET.Element]) -> Iterator[ET.Element]:
    while node in parents:
        node = parents[node]
        yield node


def _read_sheet(device) -> ET.Element:
    deadline = monotonic() + 6
    attempt = 0
    while True:
        attempt += 1
        sheet = _sheet(_tree(device))
        log.info('识别规格弹层：第 %d 次，找到=%s', attempt, sheet is not None)
        if sheet is not None:
            return sheet
        if monotonic() >= deadline:
            raise CaptureError('等待规格弹层加载超时')
        sleep(0.25)


def _close_sheet(device) -> None:
    log.info('检查并关闭规格弹层')
    deadline = monotonic() + 6
    close_clicked = False
    while True:
        root = _tree(device)
        sheet = _sheet(root)
        if sheet is None:
            try:
                _list(root, '关闭规格弹层后确认页面')
            except CaptureError as error:
                reason = str(error)
            else:
                log.info('已返回收藏列表')
                return
        else:
            reason = '规格弹层仍处于打开状态'
            if not close_clicked:
                close = next(child for child in sheet.iter('node') if _desc(child) == '关闭')
                _click(device, close)
                close_clicked = True
        if monotonic() >= deadline:
            raise CaptureError(f'等待收藏列表恢复超时：{reason}')
        sleep(0.25)


def _axes(sheet: ET.Element) -> dict[str, dict[str, _Option]]:
    scroll = next((node for node in sheet.iter('node') if _class(node) == 'android.widget.ScrollView'), None)
    recycler = next((node for node in scroll.iter('node') if _class(node) == _RECYCLER), None) if scroll is not None else None
    axes = {}
    if recycler is None:
        return axes
    if _flag(scroll, 'scrollable') or _flag(recycler, 'scrollable'):
        raise _SpecificationError('规格弹层存在滚动选项，当前无法确认全部规格')
    for group in recycler:
        if _class(group) != _LINEAR or len(group) < 2:
            raise _SpecificationError('未识别到完整的规格分组')
        name = next((_text(node) for node in group[0] if _class(node) == _TEXT_VIEW), '')
        options = {}
        containers = list(group[1])
        if any(_class(item) == _RECYCLER and _flag(item, 'scrollable') for item in containers):
            raise _SpecificationError('规格图片列表可滚动，当前无法确认全部选项')
        option_nodes = [option for item in containers
                        for option in (list(item) if _class(item) == _RECYCLER else [item])]
        for option in option_nodes:
            caption = None
            if _flag(option, 'clickable') and _desc(option).strip():
                label = option
                raw = _desc(option).strip()
                caption = next((node for node in option.iter('node')
                                if _class(node) == _TEXT_VIEW and _flag(node, 'clickable') and _text(node) == raw), None)
            else:
                label = next(
                    (node for node in option if _class(node) == _TEXT_VIEW and _flag(node, 'clickable')),
                    None,
                )
                raw = _text(label) if label is not None else ''
            if label is None:
                raise _SpecificationError('未识别到规格选项名称')
            stock = [_text(node) for node in option if node is not label and _class(node) == _TEXT_VIEW]
            sold_out = bool(_SOLD_OUT.search(raw)) or any(_SOLD_OUT.search(badge) for badge in stock)
            value = _OPTION_BADGE.sub('', _SOLD_OUT.sub('', raw).strip())
            if not value or value in options:
                raise _SpecificationError('规格选项名称为空或重复')
            options[value] = _Option(value, label, sold_out, caption)
        if not name or name in axes or not options:
            raise _SpecificationError('规格组名称为空、重复或无选项')
        axes[name] = options
    return axes


def _selected(axes: dict[str, dict[str, _Option]]) -> Sku:
    selection = []
    for name, options in axes.items():
        values = [opt.label for opt in options.values() if _flag(opt.node, 'selected')]
        if len(values) > 1:
            raise _SpecificationError(f'规格组「{name}」出现多个已选项')
        if values:
            selection.append((name, values[0]))
    return tuple(selection)


def _normalize(value: str) -> str:
    return re.sub(r'[\s;；]', '', value)


def _quote(sheet: ET.Element, spec: Sku) -> Quote:
    stamp = datetime.now().astimezone().isoformat(timespec='seconds')
    if spec:
        active = [
            match.group(1)
            for node in sheet.iter('node')
            if (match := re.fullmatch(r'已选择[：:]\s*(.+)', _text(node), flags=re.S))
        ]
        expected = _normalize(''.join(value for _, value in spec))
        log.info('核对已选组合：目标=%s，页面已选择=%s', spec, active)
        if len(active) != 1 or _normalize(active[0]) != expected:
            return Quote(spec, None, stamp, reason='已选择文案未确认这个规格组合')
    prices = [
        Decimal(match.group(1))
        for node in sheet.iter('node')
        if _class(node) == _TEXT_VIEW and (match := _PRICE.fullmatch(_text(node)))
    ]
    log.info('读取券前价：匹配数量=%d，金额=%s', len(prices), [str(value) for value in prices])
    if len(prices) != 1:
        return Quote(spec, None, stamp, reason='未读到明确且唯一的券前价')
    return Quote(spec, prices[0], stamp)


def _grey_option(image, node: ET.Element) -> bool:
    # 拼多多灰色禁选按钮仍标记 enabled=true，核对实际字色和底色。
    left, top, right, bottom = _box(node)
    inset = (bottom - top) // 4
    crop = image.crop((left + 2, top + inset, right - 2, bottom - inset)).convert('RGB')
    colors = crop.getcolors(crop.width * crop.height) or []
    counts = {color: count for count, color in colors}
    return (counts.get((248, 248, 248), 0) > crop.width * crop.height // 2
            and counts.get((210, 210, 210), 0) >= 3)


def _read_combination(device, sheet: ET.Element, spec: Sku) -> tuple[ET.Element, Quote]:
    attempted = set()
    while True:
        axes = _axes(sheet)
        selected = dict(_selected(axes))
        pending = [(name, value) for name, value in spec if selected.get(name) != value]
        log.info('切换规格：目标=%s，当前选中=%s，待选=%s', spec, selected, pending)
        if not pending and len(selected) == len(spec):
            return sheet, _quote(sheet, spec)
        stamp = datetime.now().astimezone().isoformat(timespec='seconds')
        image = device.screenshot()
        for name, value in pending:
            option = axes.get(name, {}).get(value)
            if option is None:
                continue
            target = option.caption if option.caption is not None else option.node
            unavailable = option.sold_out or not _flag(option.node, 'enabled') or _grey_option(image, target)
            if unavailable and len(pending) == 1:
                log.info('跳过当前不可选规格：%s', spec)
                return sheet, Quote(spec, None, stamp, SOLD_OUT)
            attempt = tuple(selected.items()), name, value
            if unavailable or attempt in attempted:
                continue
            attempted.add(attempt)
            _click(device, target)
            sleep(0.35)
            for _ in range(3):
                sheet = _read_sheet(device)
                if dict(_selected(_axes(sheet))).get(name) == value:
                    break
                sleep(0.2)
            break
        else:
            return sheet, Quote(spec, None, stamp, reason='未能在 App 中确认选中完整规格')


def _detail_title(device, entry: _Entry) -> str:
    _, items = _page(device, '核对需要读取完整型号的商品')
    current = next((item for item in items if item.key == entry.key and item.bounds == entry.bounds), None)
    if current is None or current.title_node is None:
        raise CaptureError('无法确认需要读取完整标题的商品行')
    log.info('规格和列表标题缺少明确型号，打开商品标题：%s', entry.label)
    _click(device, current.title_node)
    deadline = monotonic() + 6
    while True:
        root = _tree(device)
        for node in root.iter('node'):
            if node.get('resource-id') == _PACKAGE + ':id/tv_title':
                title = (_desc(node) or _text(node)).strip()
                if title:
                    device.press('back')
                    _pause_after_action()
                    _close_sheet(device)
                    return title
        if monotonic() >= deadline:
            raise CaptureError('商品详情未读到完整标题')
        sleep(0.25)


def _product(device, shop: str, entry: _Entry) -> Product:
    log.info('定位商品：店铺=%s，商品=%s，当前规格=%s，行区域=%s', shop, entry.label, entry.choice, entry.bounds)
    if entry.capsule is None:
        return Product('', shop, entry.label, issue='未找到商品的规格胶囊')
    _, visible = _page(device, '点击规格入口前复核商品行')
    current = next((item for item in visible if item.key == entry.key and item.bounds == entry.bounds), None)
    if current is None or current.capsule is None:
        raise CaptureError('收藏列表位置已变化，无法重新确认商品的规格胶囊')
    quotes = []
    issue = ''
    try:
        log.info('打开商品规格弹层')
        _click(device, current.capsule)
        sheet = _read_sheet(device)
        axes = _axes(sheet)
        log.info('规格组及选项=%s', {name: list(options) for name, options in axes.items()})
        for values in combinations(*(options for options in axes.values())):
            spec = tuple(zip(axes, values))
            sheet, quote = _read_combination(device, sheet, spec)
            log.info('规格结果：%s，券前价=%s，状态=%s', spec, quote.price,
                     quote.reason or quote.status or '已采集')
            quotes.append(quote)
    except _SpecificationError as error:
        issue = str(error)
        log.warning('商品规格需核对：%s｜%s', entry.label, issue)
    except (Exception, KeyboardInterrupt):
        try:
            _close_sheet(device)
        except Exception:
            log.exception('采集中止后未能确认规格弹层已退出')
        raise
    _close_sheet(device)
    title = entry.label
    for quote in quotes:
        brand, model, speed = name_parts(title, quote.spec)
        if quote.status != SOLD_OUT and brand and speed and not model:
            title = _detail_title(device, entry)
            break
    return Product('', shop, title, tuple(quotes), issue=issue)


def _overlap(previous: list[_Entry], current: list[_Entry]) -> int:
    def same_row(old: _Entry, new: _Entry) -> bool:
        if old.key == new.key:
            return True
        return (old.kind == new.kind == 'product' and old.label == new.label
                and ((old.clipped and not old.choice) or (new.clipped and not new.choice)))

    for count in range(min(len(previous), len(current)), 0, -1):
        old = previous[-count:]
        new = current[:count]
        if all(same_row(before, after) for before, after in zip(old, new)):
            return count
        # 新屏首行可能只剩配送文案。用它后面完整的连续行确认位置。
        if (count > 1 and current[0].clipped
                and old[0].kind == new[0].kind == 'product'
                and all(same_row(before, after) for before, after in zip(old[1:], new[1:]))):
            return count
    return 0


def scan_favorites(device, collect_ids: set[str] | None, *, shop_limit: int | None = None) -> Iterator[Product]:
    if collect_ids is not None:
        raise CaptureError('收藏商品稳定 ID 尚未接入，暂时不能执行跨次配对或比价')
    try:
        viewport, visible = _start_favorites(device)
        shop = ''
        shop_count = 0
        processed = 0
        while True:
            entries = [entry for entry in visible if entry.label]
            for entry in entries[processed:]:
                if entry.kind == 'end':
                    log.info('看到「%s」，收藏采集结束', entry.label)
                    return
                if entry.kind == 'shop':
                    if shop_limit is not None and shop_count >= shop_limit:
                        log.info('已读完前 %d 家店铺', shop_limit)
                        return
                    shop_count += 1
                    shop = entry.label
                    log.info('进入店铺分组：%s', shop)
                elif entry.capsule is None and entry.clipped:
                    if _box(viewport)[1] == int(_BOUNDS.fullmatch(entry.bounds).group(2)):
                        log.info('跳过屏幕顶部的残缺商品行：%s', entry.label)
                    else:
                        break
                else:
                    if not shop:
                        raise CaptureError('商品上方未读到所属店铺标题，请回到收藏列表顶部后重新运行')
                    collected = _product(device, shop, entry)
                    yield collected
                processed += 1
            _scroll_next(device, viewport)
            viewport, next_visible = _page(device, '列表翻页后')
            deadline = monotonic() + 6
            while True:
                sleep(0.25)
                viewport, settled = _page(device, '等待翻页布局稳定')
                if _signature(next_visible) == _signature(settled):
                    break
                next_visible = settled
                if monotonic() >= deadline:
                    raise CaptureError('收藏列表翻页后仍在变化')
            if _signature(visible) == _signature(next_visible):
                if processed != len(entries):
                    raise CaptureError('收藏底部仍有未读完的商品')
                log.info('收藏列表已读到底')
                return
            next_entries = [entry for entry in next_visible if entry.label]
            overlap = _overlap(entries, next_entries)
            log.info('检查翻页衔接：上一屏=%d，下一屏=%d，重叠=%d', len(entries), len(next_entries), overlap)
            if not overlap:
                log.info('未找到重叠商品，继续读取下一屏，当前店铺：%s', shop or '尚未识别')
            processed = max(0, processed - (len(entries) - overlap))
            visible = next_visible
    except CaptureError:
        raise
    except Exception as error:
        log.exception('设备或界面读取异常')
        raise CaptureError(f'读取拼多多收藏中断（{type(error).__name__}）') from error
