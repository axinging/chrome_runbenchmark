import os
import sys
import re


def print_help():
    help_text = """
用法:
    python rename_files.py <OLD> <NEW> [选项]

说明:
    递归遍历当前文件夹及其所有子文件夹，把文件名中所有包含 <OLD> 的部分
    替换成 <NEW>。

位置参数:
    OLD             要被替换的字符串（原名中包含的部分）
    NEW             替换成的字符串（新名中包含的部分）

选项:
    -i, --ignore-case   忽略大小写进行匹配
    -n, --dry-run       干跑模式：只打印将要重命名的结果，不实际修改
    -y, --yes           跳过确认，直接执行
    -d, --dirs          同时重命名文件夹（默认只处理文件）
    -h, --help          显示本帮助信息

示例:
    python rename_files.py "displaytodraw" "drawtoswap"
    python rename_files.py "displaytodraw" "drawtoswap" -n
    python rename_files.py "DisplayToDraw" "drawtoswap" -i -d
"""
    print(help_text)


def parse_args(argv):
    """简易参数解析，返回 (old, new, opts)"""
    opts = {
        "ignore_case": False,
        "dry_run": False,
        "yes": False,
        "dirs": False,
    }
    positional = []

    for arg in argv:
        if arg in ("-h", "--help"):
            print_help()
            sys.exit(0)
        elif arg in ("-i", "--ignore-case"):
            opts["ignore_case"] = True
        elif arg in ("-n", "--dry-run"):
            opts["dry_run"] = True
        elif arg in ("-y", "--yes"):
            opts["yes"] = True
        elif arg in ("-d", "--dirs"):
            opts["dirs"] = True
        elif arg.startswith("-") and len(arg) > 1:
            print(f"[错误] 未知选项: {arg}\n")
            print_help()
            sys.exit(1)
        else:
            positional.append(arg)

    if len(positional) != 2:
        print("[错误] 需要恰好两个参数: <OLD> <NEW>\n")
        print_help()
        sys.exit(1)

    old, new = positional
    if not old:
        print("[错误] <OLD> 不能为空\n")
        sys.exit(1)

    return old, new, opts


def build_replacer(old, ignore_case):
    """根据是否忽略大小写，返回一个替换函数 f(name) -> new_name"""
    if ignore_case:
        pattern = re.compile(re.escape(old), re.IGNORECASE)
        return lambda name: pattern.sub(
            lambda m: m.group(0).replace(m.group(0), m.group(0)), name
        )  # placeholder, replaced below
    else:
        return lambda name: name.replace(old, new)


def make_replacer(old, new, ignore_case):
    if ignore_case:
        pattern = re.compile(re.escape(old), re.IGNORECASE)
        # 用固定字符串 new 替换（不保留原大小写）
        return lambda name: pattern.sub(new, name)
    else:
        return lambda name: name.replace(old, new)


def rename_files(root, old, new, opts):
    replacer = make_replacer(old, new, opts["ignore_case"])
    count = 0

    # 先处理文件；如果需要处理文件夹，则再单独遍历
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            if old.lower() in name.lower() if opts["ignore_case"] else old in name:
                new_name = replacer(name)
                if new_name == name:
                    continue
                old_path = os.path.join(dirpath, name)
                new_path = os.path.join(dirpath, new_name)

                if os.path.exists(new_path):
                    print(f"[跳过] 目标已存在: {new_path}")
                    continue

                if opts["dry_run"]:
                    print(f"[预览] {old_path}  ->  {new_path}")
                else:
                    os.rename(old_path, new_path)
                    print(f"[重命名] {old_path}  ->  {new_path}")
                count += 1

    if opts["dirs"]:
        for dirpath, dirnames, filenames in os.walk(root, topdown=False):
            for d in dirnames:
                if old.lower() in d.lower() if opts["ignore_case"] else old in d:
                    new_d = replacer(d)
                    if new_d == d:
                        continue
                    old_path = os.path.join(dirpath, d)
                    new_path = os.path.join(dirpath, new_d)

                    if os.path.exists(new_path):
                        print(f"[跳过] 目标已存在: {new_path}")
                        continue

                    if opts["dry_run"]:
                        print(f"[预览] {old_path}  ->  {new_path}")
                    else:
                        os.rename(old_path, new_path)
                        print(f"[重命名] {old_path}  ->  {new_path}")
                    count += 1

    return count


def main():
    old, new, opts = parse_args(sys.argv[1:])

    mode = "（干跑模式，不会真正修改）" if opts["dry_run"] else ""
    print(f"当前目录: {os.getcwd()}")
    print(f"将把文件名中的 '{old}' 替换为 '{new}' {mode}")
    print("-" * 60)

    if not opts["yes"] and not opts["dry_run"]:
        ans = input("确认执行？[y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("已取消")
            return

    count = rename_files(".", old, new, opts)
    print("-" * 60)
    print(f"共处理 {count} 项")


if __name__ == "__main__":
    main()