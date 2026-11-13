import ast
import re
from pathlib import Path


def __assign_node(file_path, node_name, node_value):
    # 读取源代码
    with file_path.open('r', encoding='utf-8') as file:
        source = file.read()

    # 使用ast.parse将源代码解析为模块对象
    tree = ast.parse(source)

    # 创建一个简单的AST，例如一个赋值语句
    assign_node = ast.Assign(
        targets=[ast.Name(id=node_name, ctx=ast.Store())],
        value=node_value
    )
    assign_node.lineno = tree.body[-1].end_lineno + 1

    # 将字典作为节点添加到模块中
    tree.body.append(assign_node)

    # 将修改后的AST转换回源代码
    source_modified = ast.unparse(tree)

    # 将修改后的源代码写回文件
    with file_path.open('w', encoding='utf-8') as file:
        file.write(source_modified)


def remove_old_node(file_path: Path, node_name):
    # 读取源代码
    with file_path.open('r', encoding='utf-8') as file:
        source = file.read()

    # 使用ast.parse将源代码解析为模块对象
    tree = ast.parse(source)

    class RemoveNode(ast.NodeVisitor):
        def __init__(self):
            self.assigns_to_remove = []

        def visit_Assign(self, node):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == node_name:
                    self.assigns_to_remove.append(node)

        def visit_Module(self, node):
            self.generic_visit(node)
            for node_to_remove in self.assigns_to_remove:
                node.body.remove(node_to_remove)

    visitor = RemoveNode()
    visitor.visit(tree)

    # 将修改后的AST转换回源代码
    source_modified = ast.unparse(tree)

    # 将修改后的源代码写回文件
    with file_path.open('w', encoding='utf-8') as file:
        file.write(source_modified)


def add_new_list_node(file_path: Path, node_list_name, my_set):
    # 创建一个列表节点
    list_node = ast.List([ast.Constant(value=f'{seg}') for seg in my_set])
    __assign_node(file_path, node_list_name, list_node)


def add_new_dict_node(file_path: Path, node_dict_name, my_dict):
    # 创建一个字典节点
    dict_node = ast.Dict(keys=[ast.Constant(value=key) for key in my_dict.keys()],
                         values=[ast.Constant(value=value) for value in my_dict.values()])

    __assign_node(file_path, node_dict_name, dict_node)


def add_new_nested_dict_node(file_path: Path, node_dict_name, my_nested_dict):
    dict_node = ast.Dict(keys=[ast.Constant(value=key) for key in my_nested_dict.keys()],
                         values=[
                             ast.Dict(keys=[ast.Constant(value=key) for key in one_dict.keys()],
                                      values=[ast.Constant(value=value) for value in one_dict.values()])
                             for one_dict in my_nested_dict.values()
                         ])

    __assign_node(file_path, node_dict_name, dict_node)


def keep_chinese_and_alphanumeric(text):
    pattern = r'[^\u4e00-\u9fa5a-zA-Z0-9]+'
    return re.sub(pattern, '', text)