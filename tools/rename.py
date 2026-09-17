import os

OLD = "displaytodraw"
NEW = "drawtoswap"

def rename_files(root="."):
    # topdown=False：自底向上遍历，避免先改父目录导致子路径失效
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            if OLD in name:
                new_name = name.replace(OLD, NEW)
                old_path = os.path.join(dirpath, name)
                new_path = os.path.join(dirpath, new_name)

                if os.path.exists(new_path):
                    print(f"[跳过] 目标已存在: {new_path}")
                    continue

                os.rename(old_path, new_path)
                print(f"[重命名] {old_path}  ->  {new_path}")

    # 如果也想重命名"文件夹"本身，取消下面注释
    # for dirpath, dirnames, filenames in os.walk(root, topdown=False):
    #     for d in dirnames:
    #         if OLD in d:
    #             new_d = d.replace(OLD, NEW)
    #             os.rename(os.path.join(dirpath, d), os.path.join(dirpath, new_d))

if __name__ == "__main__":
    rename_files(".")
    print("完成")