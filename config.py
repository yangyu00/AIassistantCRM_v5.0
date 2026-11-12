# -*- encoding: utf-8 -*-
from pathlib import Path
import inspect
from collections.abc import MutableSequence
import yaml

msg = 'don\'t dynamically modify global configs!'

read_only_registry = {}


def read_only_register(base_cls):
    def decorate(func):
        cls = inspect.getmro(func)
        read_only_registry[base_cls] = cls[0]
        return func

    return decorate


@read_only_register(type(list()))
class ReadOnlyList(list):

    def __setitem__(self, key, value):
        raise Exception(msg)

    def __delitem__(self, key):
        raise Exception(msg)


@read_only_register(type(dict()))
class ReadOnlyDict(dict):

    def __setitem__(self, key, value):
        raise Exception(msg)

    def __delitem__(self, key):
        raise Exception(msg)


# 递归的设置任意的 MutableSequence 或者 dict 对象为全部只读
def recursive_read_only(obj):
    if isinstance(obj, MutableSequence):
        for i, d in enumerate(obj):
            obj[i] = recursive_read_only(d)
        obj = read_only_registry[type(obj)](obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            obj[k] = recursive_read_only(v)
        obj = read_only_registry[type(obj)](obj)

    return obj


class ImmutableYamlConfig(ReadOnlyDict):
    """
    获得不可变的yaml对象
    单例模式
    """
    instance = None

    def __new__(cls, *args, **kwargs):
        if cls.instance is None:
            cls.instance = ReadOnlyDict.__new__(cls)
            return cls.instance
        else:
            return cls.instance

    def __init__(self, path=str(Path(__file__).resolve().parent / 'config.yaml'), **kwargs):
        if not path.endswith('.yaml'):
            raise Exception('must be a yaml file!')
        with open(path, 'rb') as f:
            self.gc = yaml.load(f, Loader=yaml.SafeLoader)
        self.gc = recursive_read_only(self.gc)
        super().__init__(self.gc, **kwargs)
