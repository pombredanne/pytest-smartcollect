import io
import os
import ast
import typing
from git import Repo
from importlib import import_module
from contextlib import redirect_stdout

ListOrNone = typing.Union[list, None]
StrOrNone = typing.Union[str, None]
ListOfString = typing.List[str]


class ChangedFile(object):
    def __init__(self, change_type: str, current_filepath: str, old_filepath: StrOrNone=None, changed_lines: ListOrNone=None):
        self.change_type = change_type
        self.old_filepath = old_filepath
        self.current_filepath = current_filepath
        self.changed_lines = changed_lines


DictOfChangedFile = typing.Dict[str, ChangedFile]


class GenericVisitor(ast.NodeVisitor):
    def __init__(self):
        super(GenericVisitor, self).__init__()

    def extract(self, node) -> ListOfString:
        f = io.StringIO()

        with redirect_stdout(f):
            self.generic_visit(node)

        return f.getvalue().strip().split('\n')


class ObjectNameExtractor(GenericVisitor):
    def __init__(self):
        super(ObjectNameExtractor, self).__init__()

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name):
            print(node.func.id)

        self.generic_visit(node)


class ImportModuleNameExtractor(GenericVisitor):
    def __init__(self):
        super(ImportModuleNameExtractor, self).__init__()

    def visit_Import(self, node):
        print(node.names[0].name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        print(node.module)


def find_all_files(repo_path: str) -> DictOfChangedFile:
    all_files = {}
    for root, _, files in os.walk(repo_path):
        for f in files:
            if os.path.splitext(f)[-1] == "*.py":
                fpath = os.path.join(root, f)
                with open(fpath) as g:
                    all_files[fpath] = ChangedFile(
                        change_type='A',
                        old_filepath=None,
                        current_filepath=fpath,
                        changed_lines=[range(1, len(g.readlines()))]
                    )

    return all_files


def find_changed_files(repo: Repo) -> DictOfChangedFile:
    changed_files = {}

    current_head = repo.head.commit
    diffs = current_head.diff("HEAD~1")
    diffs_with_patch = current_head.diff("HEAD~1", create_patch=True)

    for idx, d in enumerate(diffs):
        assert d.a_path == diffs_with_patch[idx].a_path
        diff_lines_spec = diffs_with_patch[idx].diff.decode('utf-8').replace('\r', '').split('\n')[0].split('@@')[1].strip().replace('+', '').replace('-', '')
        changed_lines = None
        old_filepath = None

        if d.change_type == 'A':  # added paths
            filepath = d.a_path
            with open(filepath) as f:
                changed_lines = list(range(1, len(f.readlines())))

        elif d.change_type == 'M':  # modified paths
            filepath = d.a_path
            ranges = diff_lines_spec.split(' ')
            if len(ranges) < 2:
                start, count = ranges[0].split(',')
                changed_lines = [range(start, start + count)]

            else:
                preimage = [int(x) for x in ranges[0].split(',')]
                preimage_start = preimage[0]
                if len(preimage) > 1:
                    preimage_count = preimage[1]
                else:
                    preimage_count = 0

                postimage = [int(x) for x in ranges[1].split(',')]
                postimage_start = postimage[0]
                if len(postimage) > 1:
                    postimage_count = postimage[1]

                else:
                    postimage_count = 0

                changed_lines = [
                    range(preimage_start, preimage_start + preimage_count),
                    range(postimage_start, postimage_start + postimage_count)
                ]

        elif d.change_type == 'D':  # deleted paths
            filepath = d.a_path

        elif d.change_type == 'R':  # renamed paths
            filepath = d.b_path
            old_filepath = d.a_path

        elif d.change_type == 'T':  # changed file types
            filepath = d.b_rawpath

        else:  # something is seriously wrong...
            raise Exception("Unknown change type '%s'" % d.change_type)

        if os.path.splitext(filepath)[-1] == ".py":
            changed_files[filepath] = ChangedFile(
                d.change_type,
                filepath,
                old_filepath=old_filepath,
                changed_lines=changed_lines
            )

    return changed_files


def find_changed_members(changed_module: ChangedFile, repo_path: str) -> ListOfString:
    changed_members = []
    name_extractor = ObjectNameExtractor()

    with open(os.path.join(repo_path, changed_module.current_filepath)) as f:
        contents = f.readlines()

    total_lines = len(contents)
    module_ast = ast.parse('\n'.join(contents))
    direct_children = sorted(ast.iter_child_nodes(module_ast), key=lambda x: x.lineno)
    for idx, node in enumerate(direct_children):
        if isinstance(node, ast.Assign) or isinstance(node, ast.FunctionDef) or isinstance(node, ast.ClassDef):
            if len(direct_children) < idx + 1:
                r = range(node.lineno, direct_children[idx + 1].lineno)

            else:
                r = range(node.lineno, total_lines)

            changed_lines = set()
            for ch in changed_module.changed_lines:
                changed_lines.update(set(ch))

            if set(changed_lines).intersection(set(r)):
                if isinstance(node, ast.Assign):
                    changed_members.extend(name_extractor.extract(node))

                elif isinstance(node, ast.FunctionDef):
                    changed_members.append(node.name)

                else:
                    changed_members.append(node.name)

    return changed_members


def find_import(repo_root: str, module_path: str) -> ListOfString:
    found = []
    module_name_extractor = ImportModuleNameExtractor()

    for root, _, files in os.walk(repo_root):
        for f in files:
            if os.path.splitext(f)[-1] == ".py":
                with open(os.path.join(root, f)) as g:
                    a = ast.parse(g.read())

                for module in module_name_extractor.extract(a):
                    i = import_module(module)  # this assumes that the module is actually installed...
                    if i.__file__ == module_path:
                        found.append(i.__file__)

    return found







