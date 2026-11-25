"""按店铺采集拼多多收藏规格，管理商品配对并导出券前价。"""

import argparse
from datetime import datetime
import logging
import os
from pathlib import Path
import re
import sys
from typing import Callable, TypeVar

from pdd_compare import (
    SOLD_OUT,
    Pair,
    Product,
    comparison_rows,
    is_price_report,
    read_pairs,
    save_pairs,
    shop_role,
    sku_key,
    write_comparison_xlsx,
    write_inventory_xlsx,
)


Item = TypeVar("Item")
log = logging.getLogger("拼多多采集")
_REPORT_NAME = re.compile(
    r"([0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-[0-9]{2})(?:-([0-9]{6})| \(([0-9]+)\))?\.xlsx"
)


def _export_report(path: Path, write: Callable[[Path], Item]) -> tuple[Path, Item]:
    output = path
    version = 1
    while True:
        try:
            result = write(output)
            break
        except FileExistsError as error:
            if error.filename2 != str(output):
                raise
            version += 1
            output = path.with_name(f"{path.stem} ({version}).xlsx")

    reports = []
    for candidate in path.parent.glob("*.xlsx"):
        match = _REPORT_NAME.fullmatch(candidate.name)
        if match and not candidate.is_symlink() and candidate.is_file() and is_price_report(candidate):
            stamp, micros, suffix = match.groups()
            reports.append(((stamp, micros is None, int(micros or suffix or 0)), candidate))
    for _, old_report in sorted(reports, reverse=True)[3:]:
        old_report.unlink(missing_ok=True)
        log.info("清理旧比价报表：%s", old_report)
    return output, result


def _choose(prompt: str, items: list[Item], label: Callable[[Item], str]) -> Item | None:
    for index, item in enumerate(items, 1):
        print(f"{index}. {label(item)}")
    if not items:
        print("没有可选项。")
        return None
    while True:
        answer = input(f"{prompt}（0 返回） ").strip()
        if answer == "0":
            return None
        if answer.isascii() and answer.isdecimal() and len(answer) <= len(str(len(items))):
            index = int(answer)
            if 1 <= index <= len(items):
                return items[index - 1]
        print("请输入列表中的编号。")


def _product_label(product: Product) -> str:
    label = f"{shop_role(product.shop_name)}｜{product.shop_name or '店名未读到'}｜{product.title or '标题未读到'}"
    return f"{label}｜{product.goods_id}" if product.goods_id else label


def _spec_label(spec: tuple[tuple[str, str], ...]) -> str:
    return " / ".join(f"{name} {value}" for name, value in spec) or "无多种规格"


def _manual_spec_link(pair: Pair, device) -> bool:
    from pdd_app import scan_favorites

    target_ids = {pair.own_id, pair.competitor_id}
    products = {}
    for product in scan_favorites(device, target_ids):
        if product.goods_id in target_ids:
            products[product.goods_id] = product
        if target_ids <= products.keys():
            break

    own = products.get(pair.own_id)
    competitor = products.get(pair.competitor_id)
    if own is None or competitor is None:
        print("请确认两件商品都在收藏里。")
        return False
    if own.issue or competitor.issue:
        print(f"规格采集提示：{' / '.join(issue for issue in (own.issue, competitor.issue) if issue)}")
    own_spec = _choose("选择自家规格", [quote.spec for quote in own.quotes if quote.status != SOLD_OUT], _spec_label)
    if own_spec is None:
        return False
    competitor_spec = _choose(
        "选择对手规格", [quote.spec for quote in competitor.quotes if quote.status != SOLD_OUT], _spec_label,
    )
    if competitor_spec is None:
        return False

    own_key = sku_key(own_spec)
    competitor_key = sku_key(competitor_spec)
    if sum(sku_key(quote.spec) == own_key for quote in own.quotes) != 1 or sum(
        sku_key(quote.spec) == competitor_key for quote in competitor.quotes
    ) != 1:
        print("此规格有同名组合，无法唯一确认。")
        return False
    if any(sku_key(left) != own_key and sku_key(right) == competitor_key for left, right in pair.sku_links.items()):
        print("对手规格已配给另一组自家规格，请先移除原规格配对。")
        return False
    old_spec = next((spec for spec in pair.sku_links if sku_key(spec) == own_key), None)
    if old_spec is not None:
        del pair.sku_links[old_spec]
    pair.sku_links[own_spec] = competitor_spec
    return True


def _pair_products(device, pair_file: Path, pairs: list[Pair]) -> None:
    from pdd_app import scan_favorites

    catalog = []
    for product in scan_favorites(device, set()):
        catalog.append(product)
        print(f"读到 {_product_label(product)}")
    products_by_id = {product.goods_id: product for product in catalog}

    def pair_label(pair: Pair) -> str:
        own = products_by_id.get(pair.own_id)
        competitor = products_by_id.get(pair.competitor_id)
        own_label = _product_label(own) if own is not None else pair.own_id
        competitor_label = _product_label(competitor) if competitor is not None else pair.competitor_id
        return f"自家 {own_label} → 对手 {competitor_label}"

    while True:
        print("\n1. 新增商品配对\n2. 解除商品配对\n3. 确认规格对应\n4. 移除规格对应\n0. 退出")
        action = input("选择操作 ").strip()
        if action == "0":
            return
        if action == "1":
            own_items = [product for product in catalog if shop_role(product.shop_name) == "自家"]
            own = _choose("选择自家商品", own_items, _product_label)
            if own is None:
                continue
            options = [product for product in catalog if shop_role(product.shop_name) == "竞品"]
            competitor = _choose("选择对手商品", options, _product_label)
            if competitor is None:
                continue
            selected_ids = {own.goods_id, competitor.goods_id}
            if any({pair.own_id, pair.competitor_id} == selected_ids for pair in pairs):
                print("这两件商品已配对。")
                continue
            pairs.append(Pair(own.goods_id, competitor.goods_id))
        elif action in {"2", "3", "4"}:
            pair = _choose("选择商品配对", pairs, pair_label)
            if pair is None:
                continue
            if action == "2":
                pairs.remove(pair)
            elif action == "3":
                if not _manual_spec_link(pair, device):
                    continue
            else:
                key = _choose(
                    "选择要移除的规格对应", list(pair.sku_links),
                    lambda spec: f"{_spec_label(spec)} → {_spec_label(pair.sku_links[spec])}",
                )
                if key is None:
                    continue
                del pair.sku_links[key]
        else:
            print("请输入列表中的编号。")
            continue
        save_pairs(pair_file, pairs)
        print(f"配对已保存至 {pair_file}")


def _collect(device, collect_ids: set[str] | None, shop_limit: int | None = None) -> tuple[list[Product], bool]:
    from pdd_app import CaptureError, scan_favorites

    products = []
    try:
        for product in scan_favorites(device, collect_ids, shop_limit=shop_limit):
            products.append(product)
            print(f"读到 {_product_label(product)}")
    except CaptureError as error:
        print(f"采集提前停止，已读到 {len(products)} 件商品。原因：{error}", file=sys.stderr)
        return products, False
    return products, True


def _compare(device, pairs: list[Pair], output: Path) -> int:
    collect_ids = {goods_id for pair in pairs for goods_id in (pair.own_id, pair.competitor_id)}
    products, complete = _collect(device, collect_ids)
    rows = comparison_rows(products, pairs, scan_complete=complete)
    log.info("写入差价 Excel：%s，共 %d 行", output, len(rows))
    output, _ = _export_report(output, lambda path: write_comparison_xlsx(path, rows))
    print(f"已导出 {len(rows)} 行 · {output}")
    if not pairs:
        print("还没有配对。使用 pair 命令确认自家商品与对手商品。")
    return 0 if complete else 1


def main(argv: list[str] | None = None) -> int:
    started_at = datetime.now().astimezone()
    config_root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    default_output_dir = Path(__file__).resolve().parent / "downloads"
    parser = argparse.ArgumentParser(description="拼多多收藏商品规格比价")
    parser.add_argument("--device", metavar="SERIAL", help="多台设备连接时指定 ADB 序列号")
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan", help="按店铺采集收藏商品的规格券前价")
    scan.add_argument("--output-dir", metavar="DIR", type=Path, default=default_output_dir, help="Excel 保存目录")
    scan.add_argument("--limit", metavar="N", type=int, help="采集前 N 家店铺的全部收藏商品")
    commands.add_parser("pair", help="管理收藏商品和规格的配对")
    compare = commands.add_parser("compare", help="重新采集券前价并导出差价 Excel")
    compare.add_argument("--output-dir", metavar="DIR", type=Path, default=default_output_dir, help="Excel 保存目录")
    args = parser.parse_args(argv)
    logging.basicConfig(format="%(asctime)s [%(name)s] %(message)s", datefmt="%H:%M:%S", stream=sys.stdout)
    log.setLevel(logging.INFO)
    log.info("启动：命令=%s，店铺上限=%s，Python=%s", args.command, getattr(args, "limit", None), sys.executable)
    if args.command == "scan" and args.limit is not None and args.limit < 1:
        parser.error("--limit 必须大于 0")

    try:
        from pdd_app import CaptureError, connect_device
    except ModuleNotFoundError as error:
        if error.name != "pdd_app":
            raise
        parser.exit(1, "拼多多 App 采集模块尚未接入。\n")

    pair_file = config_root / "coin11-tb" / "pdd_pairs.json"
    try:
        device = connect_device(args.device)
        if args.command == "pair":
            _pair_products(device, pair_file, read_pairs(pair_file))
            return 0

        stamp = started_at.strftime("%Y-%m-%d_%H-%M-%S")
        output = args.output_dir.expanduser() / f"{stamp}.xlsx"
        log.info("本次结果文件=%s", output)
        if args.command == "scan":
            products, complete = _collect(device, None, args.limit)
            log.info("写入规格明细 Excel：已读取 %d 件商品，本轮正常结束=%s", len(products), complete)
            output, count = _export_report(output, lambda path: write_inventory_xlsx(path, products))
            print(f"已导出 {count} 行 · {output}")
            return 0 if complete else 1
        return _compare(device, read_pairs(pair_file), output)
    except (CaptureError, OSError, ValueError) as error:
        print(f"{error}", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\n已退出。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
