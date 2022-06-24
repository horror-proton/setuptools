"""
Create a wheel that, when installed, will make the source package 'editable'
(add it to the interpreter's path, including metadata) per PEP 660. Replaces
'setup.py develop'.

.. note::
   One of the mechanisms briefly mentioned in PEP 660 to implement editable installs is
   to create a separated directory inside ``build`` and use a .pth file to point to that
   directory. In the context of this file such directory is referred as
   *auxiliary build directory* or ``auxiliary_dir``.
"""

import logging
import os
import re
import shutil
import sys
import warnings
from contextlib import suppress
from itertools import chain
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import (
    TYPE_CHECKING,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Tuple,
    TypeVar,
    Union
)

from setuptools import Command, errors, namespaces
from setuptools.discovery import find_package_path
from setuptools.dist import Distribution

if TYPE_CHECKING:
    from wheel.wheelfile import WheelFile  # noqa

_Path = Union[str, Path]
_P = TypeVar("_P", bound=_Path)
_logger = logging.getLogger(__name__)


_STRICT_WARNING = """
New or renamed files may not be automatically picked up without a new installation.
"""

_LAX_WARNING = """
Options like `package-data`, `include/exclude-package-data` or
`packages.find.exclude/include` may have no effect.
"""


class editable_wheel(Command):
    """Build 'editable' wheel for development"""

    description = "create a PEP 660 'editable' wheel"

    user_options = [
        ("dist-dir=", "d", "directory to put final built distributions in"),
        ("dist-info-dir=", "I", "path to a pre-build .dist-info directory"),
        ("strict", None, "perform an strict installation"),
    ]

    boolean_options = ["strict"]

    def initialize_options(self):
        self.dist_dir = None
        self.dist_info_dir = None
        self.project_dir = None
        self.strict = False

    def finalize_options(self):
        dist = self.distribution
        self.project_dir = dist.src_root or os.curdir
        self.package_dir = dist.package_dir or {}
        self.dist_dir = Path(self.dist_dir or os.path.join(self.project_dir, "dist"))

    def run(self):
        self.dist_dir.mkdir(exist_ok=True)
        self._ensure_dist_info()

        # Add missing dist_info files
        bdist_wheel = self.reinitialize_command("bdist_wheel")
        bdist_wheel.write_wheelfile(self.dist_info_dir)

        self._create_wheel_file(bdist_wheel)

    def _ensure_dist_info(self):
        if self.dist_info_dir is None:
            dist_info = self.reinitialize_command("dist_info")
            dist_info.output_dir = self.dist_dir
            dist_info.finalize_options()
            dist_info.run()
            self.dist_info_dir = dist_info.dist_info_dir
        else:
            assert str(self.dist_info_dir).endswith(".dist-info")
            assert Path(self.dist_info_dir, "METADATA").exists()

    def _install_namespaces(self, installation_dir, pth_prefix):
        # XXX: Only required to support the deprecated namespace practice
        dist = self.distribution
        if not dist.namespace_packages:
            return

        src_root = Path(self.project_dir, self.pakcage_dir.get("", ".")).resolve()
        installer = _NamespaceInstaller(dist, installation_dir, pth_prefix, src_root)
        installer.install_namespaces()

    def _find_egg_info_dir(self) -> Optional[str]:
        parent_dir = Path(self.dist_info_dir).parent if self.dist_info_dir else Path()
        candidates = map(str, parent_dir.glob("*.egg-info"))
        return next(candidates, None)

    def _configure_build(
        self, name: str, unpacked_wheel: _Path, build_lib: _Path, tmp_dir: _Path
    ):
        """Configure commands to behave in the following ways:

        - Build commands can write to ``build_lib`` if they really want to...
          (but this folder is expected to be ignored and modules are expected to live
          in the project directory...)
        - Binary extensions should be built in-place (editable_mode = True)
        - Data/header/script files are not part of the "editable" specification
          so they are written directly to the unpacked_wheel directory.
        """
        # Non-editable files (data, headers, scripts) are written directly to the
        # unpacked_wheel

        dist = self.distribution
        wheel = str(unpacked_wheel)
        build_lib = str(build_lib)
        data = str(Path(unpacked_wheel, f"{name}.data", "data"))
        headers = str(Path(unpacked_wheel, f"{name}.data", "include"))
        scripts = str(Path(unpacked_wheel, f"{name}.data", "scripts"))

        # egg-info may be generated again to create a manifest (used for package data)
        egg_info = dist.reinitialize_command("egg_info", reinit_subcommands=True)
        egg_info.egg_base = str(tmp_dir)
        egg_info.ignore_egg_info_in_manifest = True

        build = dist.reinitialize_command("build", reinit_subcommands=True)
        install = dist.reinitialize_command("install", reinit_subcommands=True)

        build.build_platlib = build.build_purelib = build.build_lib = build_lib
        install.install_purelib = install.install_platlib = install.install_lib = wheel
        install.install_scripts = build.build_scripts = scripts
        install.install_headers = headers
        install.install_data = data

        install_scripts = dist.get_command_obj("install_scripts")
        install_scripts.no_ep = True

        build.build_temp = str(tmp_dir)

        build_py = dist.get_command_obj("build_py")
        build_py.compile = False
        build_py.existing_egg_info_dir = self._find_egg_info_dir()

        self._set_editable_mode()

        build.ensure_finalized()
        install.ensure_finalized()

    def _set_editable_mode(self):
        """Set the ``editable_mode`` flag in the build sub-commands"""
        dist = self.distribution
        build = dist.get_command_obj("build")
        for cmd_name in build.get_sub_commands():
            cmd = dist.get_command_obj(cmd_name)
            if hasattr(cmd, "editable_mode"):
                cmd.editable_mode = True

    def _collect_build_outputs(self) -> Tuple[List[str], Dict[str, str]]:
        files: List[str] = []
        mapping: Dict[str, str] = {}
        build = self.get_finalized_command("build")

        for cmd_name in build.get_sub_commands():
            cmd = self.get_finalized_command(cmd_name)
            if hasattr(cmd, "get_outputs"):
                files.extend(cmd.get_outputs() or [])
            if hasattr(cmd, "get_output_mapping"):
                mapping.update(cmd.get_output_mapping() or {})

        return files, mapping

    def _run_build_commands(
        self, dist_name: str, unpacked_wheel: _Path, build_lib: _Path, tmp_dir: _Path
    ) -> Tuple[List[str], Dict[str, str]]:
        self._configure_build(dist_name, unpacked_wheel, build_lib, tmp_dir)
        self.run_command("build")
        files, mapping = self._collect_build_outputs()
        self._run_install("headers")
        self._run_install("scripts")
        self._run_install("data")
        return files, mapping

    def _create_wheel_file(self, bdist_wheel):
        from wheel.wheelfile import WheelFile

        dist_info = self.get_finalized_command("dist_info")
        dist_name = dist_info.name
        tag = "-".join(bdist_wheel.get_tag())
        build_tag = "0.editable"  # According to PEP 427 needs to start with digit
        archive_name = f"{dist_name}-{build_tag}-{tag}.whl"
        wheel_path = Path(self.dist_dir, archive_name)
        if wheel_path.exists():
            wheel_path.unlink()

        # Currently the wheel API receives a directory and dump all its contents
        # inside of a wheel. So let's use a temporary directory.
        unpacked_wheel = TemporaryDirectory(suffix=archive_name)
        build_lib = TemporaryDirectory(suffix=".build-lib")
        build_tmp = TemporaryDirectory(suffix=".build-temp")

        with unpacked_wheel as unpacked, build_lib as lib, build_tmp as tmp:
            unpacked_dist_info = Path(unpacked, Path(self.dist_info_dir).name)
            shutil.copytree(self.dist_info_dir, unpacked_dist_info)
            self._install_namespaces(unpacked, dist_info.name)
            files, mapping = self._run_build_commands(dist_name, unpacked, lib, tmp)
            with WheelFile(wheel_path, "w") as wf:
                self._populate_wheel(
                    wf, dist_info.name, tag, unpacked, lib, tmp, files, mapping
                )
                wf.write_files(unpacked)

        return wheel_path

    def _run_install(self, category: str):
        has_category = getattr(self.distribution, f"has_{category}", None)
        if has_category and has_category():
            _logger.info(f"Installing {category} as non editable")
            self.run_command(f"install_{category}")

    def _populate_wheel(
        self,
        wheel: "WheelFile",
        name: str,
        tag: str,
        unpacked_dir: Path,
        build_lib: _Path,
        tmp: _Path,
        outputs: List[str],
        output_mapping: Dict[str, str],
    ):
        """Decides which strategy to use to implement an editable installation."""
        build_name = f"__editable__.{name}-{tag}"
        project_dir = Path(self.project_dir)

        if self.strict or os.getenv("SETUPTOOLS_EDITABLE", None) == "strict":
            return self._populate_link_tree(
                name, build_name, wheel, build_lib, outputs, output_mapping
            )

        # Build extensions in-place
        self.reinitialize_command("build_ext", inplace=1)
        self.run_command("build_ext")

        packages = _find_packages(self.distribution)
        has_simple_layout = _simple_layout(packages, self.package_dir, project_dir)
        if set(self.package_dir) == {""} and has_simple_layout:
            # src-layout(ish) is relatively safe for a simple pth file
            return self._populate_static_pth(name, project_dir, wheel)

        # Use a MetaPathFinder to avoid adding accidental top-level packages/modules
        self._populate_finder(name, wheel)

    def _populate_link_tree(
        self,
        name: str,
        build_name: str,
        wheel: "WheelFile",
        build_lib: _Path,
        outputs: List[str],
        output_mapping: Dict[str, str],
    ):
        """Populate wheel using the "strict" ``link tree`` strategy."""
        msg = "Strict editable install will be performed using a link tree.\n"
        _logger.warning(msg + _STRICT_WARNING)
        auxiliary_dir = _empty_dir(Path(self.project_dir, "build", build_name))
        populate = _LinkTree(
            self.distribution,
            name,
            auxiliary_dir,
            build_lib,
            outputs,
            output_mapping,
        )
        populate(wheel)

        msg = f"""\n
        Strict editable installation performed using the auxiliary directory:
            {auxiliary_dir}

        Please be careful to not remove this directory, otherwise you might not be able
        to import/use your package.
        """
        warnings.warn(msg, InformationOnly)

    def _populate_static_pth(self, name: str, project_dir: Path, wheel: "WheelFile"):
        """Populate wheel using the "lax" ``.pth`` file strategy, for ``src-layout``."""
        src_dir = self.package_dir[""]
        msg = f"Editable install will be performed using .pth file to {src_dir}.\n"
        _logger.warning(msg + _LAX_WARNING)
        populate = _StaticPth(self.distribution, name, [Path(project_dir, src_dir)])
        populate(wheel)

    def _populate_finder(self, name: str, wheel: "WheelFile"):
        """Populate wheel using the "lax" MetaPathFinder strategy."""
        msg = "Editable install will be performed using a meta path finder.\n"
        _logger.warning(msg + _LAX_WARNING)
        populate = _TopLevelFinder(self.distribution, name)
        populate(wheel)


class _StaticPth:
    def __init__(self, dist: Distribution, name: str, path_entries: List[Path]):
        self.dist = dist
        self.name = name
        self.path_entries = path_entries

    def __call__(self, wheel: "WheelFile"):
        entries = "\n".join((str(p.resolve()) for p in self.path_entries))
        contents = bytes(f"{entries}\n", "utf-8")
        wheel.writestr(f"__editable__.{self.name}.pth", contents)


class _LinkTree(_StaticPth):
    """
    Creates a ``.pth`` file that points to a link tree in the ``auxiliary_dir``.

    This strategy will only link files (not dirs), so it can be implemented in
    any OS, even if that means using hardlinks instead of symlinks.

    By collocating ``auxiliary_dir`` and the original source code, limitations
    with hardlinks should be avoided.
    """
    def __init__(
        self, dist: Distribution,
        name: str,
        auxiliary_dir: _Path,
        build_lib: _Path,
        outputs: List[str],
        output_mapping: Dict[str, str],
    ):
        self.auxiliary_dir = Path(auxiliary_dir)
        self.build_lib = Path(build_lib).resolve()
        self.outputs = outputs
        self.output_mapping = output_mapping
        self._file = dist.get_command_obj("build_py").copy_file
        super().__init__(dist, name, [self.auxiliary_dir])

    def __call__(self, wheel: "WheelFile"):
        self._create_links()
        super().__call__(wheel)

    def _normalize_output(self, file: str) -> Optional[str]:
        # Files relative to build_lib will be normalized to None
        with suppress(ValueError):
            path = Path(file).resolve().relative_to(self.build_lib)
            return str(path).replace(os.sep, '/')
        return None

    def _create_file(self, relative_output: str, src_file: str, link=None):
        dest = self.auxiliary_dir / relative_output
        if not dest.parent.is_dir():
            dest.parent.mkdir(parents=True)
        self._file(src_file, dest, link=link)

    def _create_links(self):
        link_type = "sym" if _can_symlink_files() else "hard"
        mappings = {
            self._normalize_output(k): v
            for k, v in self.output_mapping.items()
        }
        mappings.pop(None, None)  # remove files that are not relative to build_lib

        for output in self.outputs:
            relative = self._normalize_output(output)
            if relative and relative not in mappings:
                self._create_file(relative, output)

        for relative, src in mappings.items():
            self._create_file(relative, src, link=link_type)


class _TopLevelFinder:
    def __init__(self, dist: Distribution, name: str):
        self.dist = dist
        self.name = name

    def __call__(self, wheel: "WheelFile"):
        src_root = self.dist.src_root or os.curdir
        top_level = chain(_find_packages(self.dist), _find_top_level_modules(self.dist))
        package_dir = self.dist.package_dir or {}
        roots = _find_package_roots(top_level, package_dir, src_root)

        namespaces_: Dict[str, List[str]] = dict(chain(
            _find_namespaces(self.dist.packages, roots),
            ((ns, []) for ns in _find_virtual_namespaces(roots)),
        ))

        name = f"__editable__.{self.name}.finder"
        finder = _make_identifier(name)
        content = bytes(_finder_template(name, roots, namespaces_), "utf-8")
        wheel.writestr(f"{finder}.py", content)

        content = bytes(f"import {finder}; {finder}.install()", "utf-8")
        wheel.writestr(f"__editable__.{self.name}.pth", content)


def _can_symlink_files() -> bool:
    with TemporaryDirectory() as tmp:
        path1, path2 = Path(tmp, "file1.txt"), Path(tmp, "file2.txt")
        path1.write_text("file1", encoding="utf-8")
        with suppress(AttributeError, NotImplementedError, OSError):
            os.symlink(path1, path2)
            if path2.is_symlink() and path2.read_text(encoding="utf-8") == "file1":
                return True

        try:
            os.link(path1, path2)  # Ensure hard links can be created
        except Exception as ex:
            msg = (
                "File system does not seem to support either symlinks or hard links. "
                "Strict editable installs require one of them to be supported."
            )
            raise LinksNotSupported(msg) from ex
        return False


def _simple_layout(
    packages: Iterable[str], package_dir: Dict[str, str], project_dir: Path
) -> bool:
    """Return ``True`` if:
    - all packages are contained by the same parent directory, **and**
    - all packages become importable if the parent directory is added to ``sys.path``.

    >>> _simple_layout(['a'], {"": "src"}, "/tmp/myproj")
    True
    >>> _simple_layout(['a', 'a.b'], {"": "src"}, "/tmp/myproj")
    True
    >>> _simple_layout(['a', 'a.b'], {}, "/tmp/myproj")
    True
    >>> _simple_layout(['a', 'a.a1', 'a.a1.a2', 'b'], {"": "src"}, "/tmp/myproj")
    True
    >>> _simple_layout(['a', 'a.a1', 'a.a1.a2', 'b'], {"a": "a", "b": "b"}, ".")
    True
    >>> _simple_layout(['a', 'a.a1', 'a.a1.a2', 'b'], {"a": "_a", "b": "_b"}, ".")
    False
    >>> _simple_layout(['a', 'a.a1', 'a.a1.a2', 'b'], {"a": "_a"}, "/tmp/myproj")
    False
    >>> _simple_layout(['a', 'a.a1', 'a.a1.a2', 'b'], {"a.a1.a2": "_a2"}, ".")
    False
    >>> _simple_layout(['a', 'a.b'], {"": "src", "a.b": "_ab"}, "/tmp/myproj")
    False
    """
    layout = {
        pkg: find_package_path(pkg, package_dir, project_dir)
        for pkg in packages
    }
    if not layout:
        return False
    parent = os.path.commonpath([_parent_path(k, v) for k, v in layout.items()])
    return all(
        _normalize_path(Path(parent, *key.split('.'))) == _normalize_path(value)
        for key, value in layout.items()
    )


def _parent_path(pkg, pkg_path):
    """Infer the parent path containing a package, that if added to ``sys.path`` would
    allow importing that package.
    When ``pkg`` is directly mapped into a directory with a different name, return its
    own path.
    >>> _parent_path("a", "src/a")
    'src'
    >>> _parent_path("b", "src/c")
    'src/c'
    """
    parent = pkg_path[:-len(pkg)] if pkg_path.endswith(pkg) else pkg_path
    return parent.rstrip("/" + os.sep)


def _find_packages(dist: Distribution) -> Iterator[str]:
    yield from iter(dist.packages or [])

    py_modules = dist.py_modules or []
    nested_modules = [mod for mod in py_modules if "." in mod]
    if dist.ext_package:
        yield dist.ext_package
    else:
        ext_modules = dist.ext_modules or []
        nested_modules += [x.name for x in ext_modules if "." in x.name]

    for module in nested_modules:
        package, _, _ = module.rpartition(".")
        yield package


def _find_top_level_modules(dist: Distribution) -> Iterator[str]:
    py_modules = dist.py_modules or []
    yield from (mod for mod in py_modules if "." not in mod)

    if not dist.ext_package:
        ext_modules = dist.ext_modules or []
        yield from (x.name for x in ext_modules if "." not in x.name)


def _find_package_roots(
    packages: Iterable[str],
    package_dir: Mapping[str, str],
    src_root: _Path,
) -> Dict[str, str]:
    pkg_roots: Dict[str, str] = {
        pkg: _absolute_root(find_package_path(pkg, package_dir, src_root))
        for pkg in sorted(packages)
    }

    return _remove_nested(pkg_roots)


def _absolute_root(path: _Path) -> str:
    """Works for packages and top-level modules"""
    path_ = Path(path)
    parent = path_.parent

    if path_.exists():
        return str(path_.resolve())
    else:
        return str(parent.resolve() / path_.name)


def _find_virtual_namespaces(pkg_roots: Dict[str, str]) -> Iterator[str]:
    """By carefully designing ``package_dir``, it is possible to implement the logical
    structure of PEP 420 in a package without the corresponding directories.
    This function will try to find this kind of namespaces.
    """
    for pkg in pkg_roots:
        if "." not in pkg:
            continue
        parts = pkg.split(".")
        for i in range(len(parts) - 1, 0, -1):
            partial_name = ".".join(parts[:i])
            path = Path(find_package_path(partial_name, pkg_roots, ""))
            if not path.exists():
                yield partial_name


def _find_namespaces(
    packages: List[str], pkg_roots: Dict[str, str]
) -> Iterator[Tuple[str, List[str]]]:
    for pkg in packages:
        path = find_package_path(pkg, pkg_roots, "")
        if Path(path).exists() and not Path(path, "__init__.py").exists():
            yield (pkg, [path])


def _remove_nested(pkg_roots: Dict[str, str]) -> Dict[str, str]:
    output = dict(pkg_roots.copy())

    for pkg, path in reversed(list(pkg_roots.items())):
        if any(
            pkg != other and _is_nested(pkg, path, other, other_path)
            for other, other_path in pkg_roots.items()
        ):
            output.pop(pkg)

    return output


def _is_nested(pkg: str, pkg_path: str, parent: str, parent_path: str) -> bool:
    """
    Return ``True`` if ``pkg`` is nested inside ``parent`` both logically and in the
    file system.
    >>> _is_nested("a.b", "path/a/b", "a", "path/a")
    True
    >>> _is_nested("a.b", "path/a/b", "a", "otherpath/a")
    False
    >>> _is_nested("a.b", "path/a/b", "c", "path/c")
    False
    """
    norm_pkg_path = _normalize_path(pkg_path)
    rest = pkg.replace(parent, "").strip(".").split(".")
    return (
        pkg.startswith(parent)
        and norm_pkg_path == _normalize_path(Path(parent_path, *rest))
    )


def _normalize_path(filename: _Path) -> str:
    """Normalize a file/dir name for comparison purposes"""
    # See pkg_resources.normalize_path
    file = os.path.abspath(filename) if sys.platform == 'cygwin' else filename
    return os.path.normcase(os.path.realpath(os.path.normpath(file)))


def _empty_dir(dir_: _P) -> _P:
    """Create a directory ensured to be empty. Existing files may be removed."""
    shutil.rmtree(dir_, ignore_errors=True)
    os.makedirs(dir_)
    return dir_


def _make_identifier(name: str) -> str:
    """Make a string safe to be used as Python identifier.
    >>> _make_identifier("12abc")
    '_12abc'
    >>> _make_identifier("__editable__.myns.pkg-78.9.3_local")
    '__editable___myns_pkg_78_9_3_local'
    """
    safe = re.sub(r'\W|^(?=\d)', '_', name)
    assert safe.isidentifier()
    return safe


class _NamespaceInstaller(namespaces.Installer):
    def __init__(self, distribution, installation_dir, editable_name, src_root):
        self.distribution = distribution
        self.src_root = src_root
        self.installation_dir = installation_dir
        self.editable_name = editable_name
        self.outputs = []

    def _get_target(self):
        """Installation target."""
        return os.path.join(self.installation_dir, self.editable_name)

    def _get_root(self):
        """Where the modules/packages should be loaded from."""
        return repr(str(self.src_root))


_FINDER_TEMPLATE = """\
import sys
from importlib.machinery import ModuleSpec
from importlib.machinery import all_suffixes as module_suffixes
from importlib.util import spec_from_file_location
from itertools import chain
from pathlib import Path

MAPPING = {mapping!r}
NAMESPACES = {namespaces!r}
PATH_PLACEHOLDER = {name!r} + ".__path_hook__"


class _EditableFinder:  # MetaPathFinder
    @classmethod
    def find_spec(cls, fullname, path=None, target=None):
        for pkg, pkg_path in reversed(list(MAPPING.items())):
            if fullname.startswith(pkg):
                rest = fullname.replace(pkg, "").strip(".").split(".")
                return cls._find_spec(fullname, Path(pkg_path, *rest))

        return None

    @classmethod
    def _find_spec(cls, fullname, candidate_path):
        init = candidate_path / "__init__.py"
        candidates = (candidate_path.with_suffix(x) for x in module_suffixes())
        for candidate in chain([init], candidates):
            if candidate.exists():
                return spec_from_file_location(fullname, candidate)


class _EditableNamespaceFinder:  # PathEntryFinder
    @classmethod
    def _path_hook(cls, path):
        if path == PATH_PLACEHOLDER:
            return cls
        raise ImportError

    @classmethod
    def _paths(cls, fullname):
        # Ensure __path__ is not empty for the spec to be considered a namespace.
        return NAMESPACES[fullname] or MAPPING.get(fullname) or [PATH_PLACEHOLDER]

    @classmethod
    def find_spec(cls, fullname, target=None):
        if fullname in NAMESPACES:
            spec = ModuleSpec(fullname, None, is_package=True)
            spec.submodule_search_locations = cls._paths(fullname)
            return spec
        return None

    @classmethod
    def find_module(cls, fullname):
        return None


def install():
    if not any(finder == _EditableFinder for finder in sys.meta_path):
        sys.meta_path.append(_EditableFinder)

    if not NAMESPACES:
        return

    if not any(hook == _EditableNamespaceFinder._path_hook for hook in sys.path_hooks):
        # PathEntryFinder is needed to create NamespaceSpec without private APIS
        sys.path_hooks.append(_EditableNamespaceFinder._path_hook)
    if PATH_PLACEHOLDER not in sys.path:
        sys.path.append(PATH_PLACEHOLDER)  # Used just to trigger the path hook
"""


def _finder_template(
    name: str, mapping: Mapping[str, str], namespaces: Dict[str, List[str]]
) -> str:
    """Create a string containing the code for the``MetaPathFinder`` and
    ``PathEntryFinder``.
    """
    mapping = dict(sorted(mapping.items(), key=lambda p: p[0]))
    return _FINDER_TEMPLATE.format(name=name, mapping=mapping, namespaces=namespaces)


class InformationOnly(UserWarning):
    """Currently there is no clear way of displaying messages to the users
    that use the setuptools backend directly via ``pip``.
    The only thing that might work is a warning, although it is not the
    most appropriate tool for the job...
    """


class LinksNotSupported(errors.FileError):
    """File system does not seem to support either symlinks or hard links."""
