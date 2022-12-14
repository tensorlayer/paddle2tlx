# coding: utf-8
import os.path as osp
import sys


def get_dep_file_path(current_file_path, from_level, from_str):
    """ 根据from信息获取依赖包所在文件。如果from字符串中存在相对路径（出现"."），
        则根据相对路径找到相应的文件；反之，执行import语句找到相应依赖的文件。
        Args:
            current_file_path (str): from信息所在文件的路径。
            from_level (int): from信息中父目录级别数。
            from_str (str): from信息中依赖包名字。
    """
    if from_level > 0:
        while from_level > 0:
            current_file_path, folder_or_file = osp.split(current_file_path)
            from_level -= 1
        if from_str is None:
            import_file_path = osp.join(current_file_path, "paddle2tlx/__init__.py")
        else:
            current_file_path = osp.join(current_file_path,
                                         osp.join(*from_str.split(".")))
            if osp.exists(current_file_path + ".py"):
                import_file_path = current_file_path + ".py"
            else:
                import_file_path = osp.join(current_file_path, "paddle2tlx/__init__.py")
    else:
        current_abs_path = osp.dirname(current_file_path)
        sys.path.append(current_abs_path)
        if len(from_str.split(".")) == 1:
            key_str = from_str
            exec("import {}".format(key_str))
        else:
            from_seg = from_str.split(".")
            from_str = ".".join(from_seg[0:-1])
            key_str = from_seg[-1]
            exec("from {} import {}".format(from_str, key_str))
        sys.path.pop(-1)
        import_file_path = locals()[key_str].__file__
    return import_file_path


def add_line_continuation_symbol(code):
    code_list = code.split("\n")
    for i, line in enumerate(code_list):
        if line.strip().endswith("="):
            code_list[i] = line + "\\"
    return "\n".join(code_list)
