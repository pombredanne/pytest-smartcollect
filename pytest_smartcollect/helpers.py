import re
import os
import sys
import ast
import pytest
import typing
import logging
from git import Repo
from importlib import import_module
from chardet import UniversalDetector
from abc import abstractmethod

ListOrNone = typing.Union[list, None]
StrOrNone = typing.Union[str, None]
ListOfString = typing.List[str]
DictOfListOfString = typing.Dict[str, ListOfString]
DictOfString = typing.Dict[str, str]
ListOfTestItem = typing.List[pytest.Item]


class ModuleMember(object):
    def __init__(self, module_path, name, depends_on=None, changed=False, color="white"):
        self.module_path = module_path
        self.name = name
        self.depends_on = depends_on
        self.changed = changed
        self.color = color

    def reset_color(self):
        self.color = "white"


class ChangedFile(object):
    def __init__(self, change_type: str, current_filepath: str, old_filepath: StrOrNone=None, changed_lines: ListOrNone=None):
        self.change_type = change_type
        self.old_filepath = old_filepath
        self.current_filepath = current_filepath
        self.changed_lines = changed_lines


DictOfChangedFile = typing.Dict[str, ChangedFile]


class ModuleInfoExtractor(ast.NodeVisitor):
    def __init__(self):
        super(ModuleInfoExtractor, self).__init__()
        self.info = {}
        self.reset_info()

    def reset_info(self):
        self.info["imports"] = []
        self.info["members"] = {
            "functions": {},
            "classes": {},
            "assignments": {}
        }

    def extract(self, module_contents: str) -> dict:
        self.reset_info()
        node = ast.parse(module_contents)
        self.generic_visit(node)
        return self.info

    def visit_Import(self, node):
        self.info["imports"].append((node.names[0].name, [], 0))

    def visit_ImportFrom(self, node):
        names = list(map(lambda x: x.name, node.names))
        self.info["imports"].append((node.module, names, node.level))

    def visit_Assign(self, node):
        target_names = []
        used_names = []
        for t in node.targets:
            if isinstance(t, ast.Name):
                target_names.append(t.id)
            else:
                for child in ast.walk(t):
                    if isinstance(child, ast.Name):
                        target_names.append(child.id)

        for child in ast.walk(node.value):
            name = self._extract_name_from_node(child)
            if name is not None:
                used_names.append(name)

        for tn in target_names:
            self.info["members"]["assignments"][tn] = {"lineno": node.lineno, "uses": used_names}

    def visit_ClassDef(self, node):
        used_names = []
        for base in node.bases:
            name = self._extract_name_from_node(base)
            if name is not None:
                used_names.append(name)

        used_names.extend(self._process_block_body(node))
        self.info["members"]["classes"][node.name] = {"lineno": node.lineno, "uses": used_names}

    def visit_FunctionDef(self, node):
        self._process_function(node)

    def visit_AsyncFunctionDef(self, node):
        self._process_function(node)

    def _extract_name_from_node(self, node) -> str:
        name = None
        if isinstance(node, ast.Name):
            if ast.Attribute in [type(x) for x in ast.iter_child_nodes(node)]:
                for child in ast.walk(node):
                    if isinstance(child, ast.Attribute):
                        name = child.attr
                        break

            else:
                name = node.id

        elif isinstance(node, ast.Attribute):
            name = node.attr

        return name

    def _process_function(self, node):
        if len(node.decorator_list) > 0:
            decorator_names = []
            for dec in node.decorator_list:
                name = self._extract_name_from_node(dec)
                if name is not None:
                    decorator_names.append(name)

            self.info["members"]["functions"][node.name] = {
                "uses": decorator_names
            }

        else:
            self.info["members"]["functions"][node.name] = {"uses":[]}

        self.info["members"]["functions"][node.name]["uses"].extend(self._process_block_body(node))
        self.info["members"]["functions"][node.name]["args"] = self._process_args(node)
        self.info["members"]["functions"][node.name]["lineno"] = node.lineno

    def _process_block_body(self, node):
        used_names = []
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                name = self._extract_name_from_node(child.func)
                if name is not None:
                    used_names.append(name)

        return used_names

    def _process_args(self, node):
        return [x.arg for x in node.args.args]


class SmartCollector(object):
    def __init__(self, rootdir: str, lastfailed: ListOfString, ignore_source: ListOfString, commit_range: int, diff_current_head_with_branch: str, allow_preemptive_failures: bool, logger: logging.Logger):
        self.rootdir = rootdir
        self.lastfailed = lastfailed
        self.ignore_source = ignore_source
        self.commit_range = commit_range
        self.diff_current_head_with_branch = diff_current_head_with_branch
        self.allow_preemptive_failures = allow_preemptive_failures
        self.logger = logger
        self.packages = []
        self.encoding_detector = UniversalDetector()

    def read_file(self, fpath):
        self.encoding_detector.reset()

        f = open(fpath, "rb")
        for line in f.readlines():
            self.encoding_detector.feed(line)
            if self.encoding_detector.done:
                break
        f.close()

        if self.encoding_detector.result['encoding'] is None:
            enc = 'utf-8'

        else:
            enc = self.encoding_detector.result['encoding'].lower()

        with open(fpath, encoding=enc) as f:
            lines = f.readlines()

        contents = ''.join(lines)
        linecount = len(lines)

        try:
            ast.parse(contents)

        except Exception as e:
            raise Exception("Couldn't read file '%s' -- %s" % (fpath, str(e)))

        return contents, linecount

    def find_git_repo_root(self, dir: str) -> str:
        if ".git" in os.listdir(dir):
            return dir

        else:
            if os.path.dirname(dir) == dir:
                raise Exception("No git repo found relative to the pytest rootdir")

            else:
                return self.find_git_repo_root(os.path.dirname(dir))

    @staticmethod
    def find_packages(dir: str) -> ListOfString:
        packages = []
        is_valid_package = lambda x: True if os.path.isdir(x) and '__init__.py' in os.listdir(x) else False

        for root, _, _ in os.walk(dir):
            abs_root = os.path.abspath(root)

            if is_valid_package(abs_root):
                packages.append(abs_root)

        return packages

    def find_all_files(self, repo_path: str) -> DictOfChangedFile:
        all_files = {}
        for root, _, files in os.walk(repo_path):
            for f in files:
                fpath = os.path.join(root, f)
                if os.path.splitext(f)[-1] == ".py" and not self.should_ignore_source_file(fpath):
                    contents, linecount = self.read_file(fpath)
                    all_files[fpath] = ChangedFile(
                        change_type='A',
                        old_filepath=None,
                        current_filepath=fpath,
                        changed_lines=[range(1, linecount)]
                    )

        return all_files

    def find_changed_files(self, repo: Repo, repo_path: str) -> (DictOfChangedFile, DictOfChangedFile, DictOfChangedFile, DictOfChangedFile, DictOfChangedFile):
        changed_files = {
            'A': {},
            'M': {},
            'D': {},
            'R': {},
            'T': {}
        }

        current_head = repo.head.commit
        previous_commits = repo.commit("%s~%d" % (self.diff_current_head_with_branch, self.commit_range))
        diffs = previous_commits.diff(current_head)
        diffs_with_patch = previous_commits.diff(current_head, create_patch=True)

        for idx, d in enumerate(diffs):
            diff_text = diffs_with_patch[idx].diff.decode('utf-8').replace('\r', '')
            if re.match('^Binary files.*', diff_text) or len(diff_text) == 0:  # TODO: figure out if there are any other special cases where the diff information is non-standard
                continue
            diff_lines_spec = diff_text.split('\n')[0].split('@@')[1].strip().replace('+', '').replace('-', '')
            changed_lines = None
            old_filepath = None

            if d.change_type == 'A':  # added paths
                filepath = os.path.join(repo_path, d.a_path)
                if os.path.splitext(filepath)[-1] != '.py':
                    continue

                _, linecount = self.read_file(filepath)
                changed_lines = [range(1, linecount)]

            elif d.change_type == 'M':  # modified paths
                filepath = os.path.join(repo_path, d.a_path)
                if os.path.splitext(filepath)[-1] != '.py':
                    continue
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
                filepath = os.path.join(repo_path, d.a_path)
                if os.path.splitext(filepath)[-1] != '.py':
                    continue

            elif d.change_type == 'R':  # renamed paths
                filepath = os.path.join(repo_path, d.b_path)
                if os.path.splitext(filepath)[-1] != '.py':
                    continue
                old_filepath = os.path.join(repo_path, d.a_path)
                _, linecount = self.read_file(filepath)
                changed_lines = [range(1, linecount)]

            elif d.change_type == 'T':  # changed file types
                filepath = os.path.join(repo_path, d.b_rawpath)
                if os.path.splitext(filepath)[-1] != '.py':
                    continue
                old_filepath = os.path.join(repo_path, d.a_path)
                _, linecount = self.read_file(filepath)
                changed_lines = [range(1, linecount)]

            else:  # something is seriously wrong...
                raise Exception("Unknown change type '%s'" % d.change_type)

            # we only care about python files here
            if os.path.splitext(filepath)[-1] == ".py":
                if os.sep == "\\":
                    filepath = filepath.replace('/', os.sep)

                    if old_filepath is not None:
                        old_filepath = old_filepath.replace('/', os.sep)

                changed_files[d.change_type][filepath] = ChangedFile(
                    d.change_type,
                    filepath,
                    old_filepath=old_filepath,
                    changed_lines=changed_lines
                )

        return changed_files['A'], changed_files['M'], changed_files['D'], changed_files['R'], changed_files['T']

    def should_ignore_source_file(self, source_file: str) -> bool:
        if self.ignore_source is not None:
            for ign in self.ignore_source:
                if os.path.commonpath([source_file, ign]) == ign:
                    return True

        return False

    def find_changed_members(self, changed_module: ChangedFile, repo_path: str) -> ListOfString:
        # find all changed members of changed_module
        changed_members = []

        contents, total_lines = self.read_file(os.path.join(repo_path, changed_module.current_filepath))
        # module_ast = ast.parse(contents)
        # direct_children = list(ast.iter_child_nodes(module_ast))

        module_info_extractor = ModuleInfoExtractor()
        module_info = module_info_extractor.extract(contents)
        direct_children = []
        for k, v in module_info["members"]["classes"].items():
            direct_children.append((k, v["lineno"]))

        for k, v in module_info["members"]["functions"].items():
            direct_children.append((k, v["lineno"]))

        for k, v in module_info["members"]["assignments"].items():
            direct_children.append((k, v["lineno"]))

        direct_children = sorted(direct_children, key=lambda x: x[1])

        # get a set of all changed lines in changed_module
        changed_lines = set()
        for ch in changed_module.changed_lines:
            changed_lines.update(set(ch))

        # the direct children of the module correspond to the imported names in test files
        for (idx, (node_name, lineno)) in enumerate(direct_children):
            try:
                r = range(lineno, direct_children[idx + 1][1])

            except IndexError:
                r = range(lineno, total_lines)

            if set(changed_lines).intersection(set(r)):
                changed_members.extend(node_name)

        return changed_members

    @staticmethod
    def find_fully_qualified_module_name(path: str) -> str:
        parts = [os.path.splitext(os.path.basename(path))[0]]

        while "__init__.py" in os.listdir(os.path.dirname(path)):
            parts.insert(0, os.path.basename(os.path.dirname(path)))
            path = os.path.dirname(path)

        return ".".join(parts)

    @staticmethod
    def file_in_project(dir, f):
        if dir not in os.path.commonpath([dir, f]):
            return False

        return True

    @staticmethod
    def add_key_and_value_to_linked_list(module_path, member, m):
        if module_path not in m.keys():
            m[module_path] = [member]

        else:
            if member not in m[module_path]:
                m[module_path].append(member)

    def dependencies_changed(self, path: str, object_name: str, change_map: DictOfListOfString, unchanged_map: DictOfListOfString, visited_map: DictOfListOfString, chain: ListOfString) -> bool:
        git_repo_root = self.find_git_repo_root(self.rootdir)
        chain.append("%s::%s" % (path, object_name))

        # if this module and object have been visited before but aren't yet marked changed or unchanged, then we have detected a dependency cycle
        if path in visited_map.keys() and object_name in visited_map[path] and chain.count("%s::%s" % (path, object_name)) > 1:
            raise Exception("Dependency cycle detected: " + ' -> '.join(chain))

        else:
            self.add_key_and_value_to_linked_list(path, object_name, visited_map)

        if path in change_map.keys() and object_name in change_map[path]: # if we've seen this file before and already know it to be changed, just return True
            return True

        if path in unchanged_map.keys() and object_name in unchanged_map[path]:
            chain.pop()
            return False

        if not self.file_in_project(git_repo_root, path):  # if the file is outside of the project, don't bother checking it or any of its dependencies
            self.add_key_and_value_to_linked_list(path, object_name, unchanged_map)
            self.add_key_and_value_to_linked_list(path, object_name, visited_map)
            chain.pop()
            return False

        assert object_name in visited_map[path]

        # otherwise, recursively check the dependencies of this file for other known changes
        contents, _ = self.read_file(path)
        module_info_extractor = ModuleInfoExtractor()
        module_info = module_info_extractor.extract(contents)

        # find locally changed members
        locally_changed = []
        if path in change_map.keys():
            locally_changed = change_map[path]

        # find the module member of interest in the ast
        # obj = None
        # definition_extractor = DefinitionNodeExtractor()
        # assignment_target_extractor = AssignmentExtractor()
        #
        # definition_nodes = definition_extractor.extract(module_ast)
        # assignment_nodes = assignment_target_extractor.extract(module_ast)

        # for definition in definition_nodes:
        #     if definition.name == object_name:
        #         obj = definition
        #         break
        #
        #     else:
        #         continue
        #
        # for (assignment_name, assignment_value) in assignment_nodes:
        #     if assignment_name == object_name:
        #         obj = assignment_value
        #         break
        #
        #     else:
        #         continue
        #
        # if obj is None:  # if the object wasn't a definition and is unchanged, assume that there are no further dependencies in the chain
        #     self.add_key_and_value_to_linked_list(path, object_name, unchanged_map)
        #     chain.pop()
        #     return False

        # extract imports
        imported_names_and_modules = {}
        # imne = ImportModuleNameExtractor()
        # extracted_imports = imne.extract(module_ast)

        for (module_name, imported_names, import_level) in module_info["imports"]:
            if module_name in sys.builtin_module_names: # we can safely assume that builtin module changes aren't relevant
                continue

            if module_name is None:  # here we need to find the fully qualified module name for a package relative import
                assert import_level > 0
                module_name = self.find_fully_qualified_module_name(os.path.dirname(path))

            else:
                if import_level > 0: # another package relative import situation
                    module_name = [module_name]
                    src_path = path
                    while import_level > 0:
                        src_path = os.path.dirname(src_path)
                        module_name.insert(0, os.path.basename(src_path))
                        import_level -= 1

                    module_name = '.'.join(module_name)

            if len(imported_names) == 0 or '*' in imported_names:
                imp = import_module(module_name)
                imported_names = dir(imp)

            i = import_module(module_name)

            for imported_name in imported_names:
                o = getattr(i, imported_name)

                if hasattr(o, '__module__') and o.__module__ not in sys.builtin_module_names and o.__module__ is not None:
                    f = import_module(o.__module__).__file__

                else:
                    f = None

                if hasattr(i, '__file__') and self.file_in_project(git_repo_root, i.__file__):
                    self.add_key_and_value_to_linked_list(imported_name, i.__file__, imported_names_and_modules)

                if f is not None and self.file_in_project(git_repo_root, f):
                    self.add_key_and_value_to_linked_list(imported_name, f, imported_names_and_modules)

        # check base classes recursively
        base_class_name_extractor = BaseClassNameExtractor()
        if isinstance(obj, ast.ClassDef):
            self.logger.warning("processing base classes for %s::%s" % (path, object_name))
            for base_name in base_class_name_extractor.extract(obj):
                self.logger.warning(base_name)
                if base_name in imported_names_and_modules.keys():
                    base_class_module_paths = imported_names_and_modules[base_name]
                    for base_class_path in base_class_module_paths:
                        if self.dependencies_changed(base_class_path, base_name, change_map, unchanged_map, visited_map, chain):
                            self.add_key_and_value_to_linked_list(base_class_path, base_name, change_map)
                            self.add_key_and_value_to_linked_list(path, object_name, change_map)
                            return True

                elif base_name in [x.name for x in definition_nodes]:
                    if self.dependencies_changed(path, base_name, change_map, unchanged_map, visited_map, chain):
                        self.add_key_and_value_to_linked_list(path, base_name, change_map)
                        self.add_key_and_value_to_linked_list(path, object_name, change_map)
                        return True

        # extract call objects from obj
        object_name_extractor = ObjectNameExtractor()
        used_names = set(object_name_extractor.extract(obj))

        self.logger.warning("used names in %s::%s: " % (path, object_name) + ', '.join(used_names))
        for name in used_names:

            if name == object_name:  # to avoid infinite recursion when a class invokes it's own class methods or if a recursive function calls itself
                continue

            if name in locally_changed:
                if path in change_map.keys() and name in change_map[path]:
                    self.add_key_and_value_to_linked_list(path, object_name, change_map)
                    return True

            if name in imported_names_and_modules.keys():
                for module_path in imported_names_and_modules[name]:
                    if self.dependencies_changed(module_path, name, change_map, unchanged_map, visited_map, chain):
                        self.add_key_and_value_to_linked_list(module_path, name, change_map)
                        self.add_key_and_value_to_linked_list(path, object_name, change_map)
                        return True

        self.add_key_and_value_to_linked_list(path, object_name, unchanged_map)
        chain.pop()
        return False

    def run(self, items):
        log_records = []
        git_repo_root = self.find_git_repo_root(self.rootdir)
        self.packages = self.find_packages(git_repo_root)

        for p in self.packages:
            sys.path.insert(0, p)

        try:
            repo = Repo(git_repo_root)

            total_commits_on_head = len(list(repo.iter_commits("HEAD")))

            if self.diff_current_head_with_branch == repo.active_branch.name and total_commits_on_head < 2:
                added_files = self.find_all_files(git_repo_root)
                modified_files = {}
                deleted_files = {}
                renamed_files = {}
                changed_filetype_files = {}

            else:  # inspect the diff
                added_files, modified_files, deleted_files, renamed_files, changed_filetype_files = self.find_changed_files(repo, git_repo_root)

            changed_to_py = {}
            for changed_filetype in changed_filetype_files.values():
                if os.path.splitext(changed_filetype.current_filepath) == ".py":
                    changed_to_py[changed_filetype.current_filepath] = changed_filetype

            changed_files = {}
            changed_files.update(changed_to_py)
            changed_files.update(modified_files)
            changed_files.update(renamed_files)
            changed_files.update(added_files)

            # ignore anything explicitly set in --ignore-source flags
            changed_files = {k: v for k, v in changed_files.items() if not self.should_ignore_source_file(k)}

            # determine all changed members of each of the changed files (if applicable)
            changed_members_and_modules = {
                path: self.find_changed_members(ch, git_repo_root) for path, ch in changed_files.items()
            }

            test_count = 0
            fixture_map = {}
            ast_map = {}
            unchanged_members_and_modules = {}
            module_info_extractor = ModuleInfoExtractor()

            for test in items:
                test_name = test.name.split('[')[0]  # TODO: figure out a better way to handle test names of parameterized tests

                # if the test is new, run it anyway
                if str(test.fspath) in changed_files.keys() and changed_files[str(test.fspath)].change_type == 'A':
                    log_records.append(
                        ('RUN', test.nodeid, "New test")
                    )
                    self.logger.info("Test '%s' is new, so will be run regardless of changes to the code it tests" % test.nodeid)
                    test_count += 1
                    continue

                # if the test failed in the last run, run it anyway
                if test.nodeid in self.lastfailed:
                    log_records.append(
                        ('RUN', test.nodeid, "Failed on last run")
                    )
                    self.logger.info(
                        "Test '%s' failed on the last run, so will be run regardless of changes" % test.nodeid)
                    test_count += 1
                    continue

                # if the test is already skipped, just ignore it
                if test.get_marker('skip'):
                    log_records.append(
                        ('SKIP', test.nodeid, "Found skip marker")
                    )
                    self.logger.info("Found skip marker on test '%s' -- ignoring" % test.nodeid)
                    continue

                contents, _ = self.read_file(str(test.fspath))
                test_module_info = module_info_extractor.extract(contents)

                # check dependencies within any defined fixtures
                # if str(test.fspath) in ast_map.keys():
                #     test_file_ast = ast_map[str(test.fspath)]
                #
                # else:
                #     contents, _ = self.read_file(str(test.fspath))
                #     test_file_ast = ast.parse(contents)
                #     ast_map[str(test.fspath)] = test_file_ast

                test_node = None
                for child in ast.iter_child_nodes(test_file_ast):
                    if isinstance(child, ast.ClassDef):
                        for subchild in ast.iter_child_nodes(child):
                            if isinstance(subchild, ast.FunctionDef) and subchild.name == test_name:
                                test_node = subchild
                                break

                        if test_node is not None:
                            break

                    elif isinstance(child, ast.FunctionDef) and child.name == test_name:
                        test_node = child
                        break

                assert test_node is not None

                if str(test.fspath) not in fixture_map.keys():
                    fixture_extractor = FixtureExtractor()
                    fixtures = fixture_extractor.extract(test_file_ast)

                    fixture_map[str(test.fspath)] = fixtures

                found_changed_fixture = False
                for fixture in fixture_map[str(test.fspath)]:
                    for arg in test_node.args.args:
                        if arg.arg == fixture.name and self.dependencies_changed(str(test.fspath), fixture.name, changed_members_and_modules, unchanged_members_and_modules, {}, []):
                            log_records.append(
                                ('RUN', test.nodeid, "Uses changed fixture")
                            )
                            self.logger.info("Test '%s' will run because it uses a changed fixture (%s)" % (
                            test.nodeid, fixture.name))
                            test_count += 1
                            found_changed_fixture = True
                            break

                    if found_changed_fixture:
                        break

                if found_changed_fixture:
                    continue

                # otherwise, check the dependency chain from inside the test function
                chain = []
                if self.dependencies_changed(str(test.fspath), test_name, changed_members_and_modules, unchanged_members_and_modules, {}, chain):
                    log_records.append(
                        ('RUN', test.nodeid, "Dependency changed: " + ' -> '.join(chain))
                    )
                    self.logger.info(
                        "Test '%s' will run because one of it's dependencies changed (%s)" % (
                        test.nodeid, ' -> '.join(chain)))
                    test_count += 1
                    continue

                else:
                    log_records.append(
                        ('SKIP', test.nodeid, "Unchanged")
                    )
                    self.logger.info("Test '%s' doesn't touch new or modified code -- SKIPPING" % test.nodeid)
                    skip = pytest.mark.skip(reason="This test doesn't touch new or modified code")
                    test.add_marker(skip)

            # TODO: add option to write to csv
            import csv
            with open("results.csv", "w") as csvfile:
                csvwriter = csv.writer(csvfile)
                for row in log_records:
                    csvwriter.writerow(list(row))

            self.logger.warning("Total tests selected to run: " + str(test_count))
            self._revert_syspath()

        except Exception as e:
            self._handle_exception(str(e))

    def _handle_exception(self, msg):
        self._revert_syspath()
        raise Exception(msg)

    def _revert_syspath(self):
        for _ in range(0, len(self.packages)):
            sys.path.pop(0)






