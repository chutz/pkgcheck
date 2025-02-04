import os
import stat
from collections import defaultdict

from pkgcore.ebuild.atom import MalformedAtom
from pkgcore.ebuild.atom import atom as atom_cls
from snakeoil.chksum import get_chksums
from snakeoil.osutils import listdir, pjoin, sizeof_fmt
from snakeoil.strings import pluralism as _pl

from .. import base, git, results, sources
from . import Check

allowed_filename_chars = "a-zA-Z0-9._-+:"
allowed_filename_chars_set = set()
allowed_filename_chars_set.update(chr(x) for x in range(ord('a'), ord('z')+1))
allowed_filename_chars_set.update(chr(x) for x in range(ord('A'), ord('Z')+1))
allowed_filename_chars_set.update(chr(x) for x in range(ord('0'), ord('9')+1))
allowed_filename_chars_set.update([".", "-", "_", "+", ":"])


class MismatchedPN(results.PackageResult, results.Error):
    """Ebuilds that have different names than their parent directory."""

    def __init__(self, ebuilds, **kwargs):
        super().__init__(**kwargs)
        self.ebuilds = tuple(ebuilds)

    @property
    def desc(self):
        return "mismatched package name%s: [ %s ]" % (
            _pl(self.ebuilds), ', '.join(self.ebuilds))


class InvalidPN(results.PackageResult, results.Error):
    """Ebuilds that have invalid package names."""

    def __init__(self, ebuilds, **kwargs):
        super().__init__(**kwargs)
        self.ebuilds = tuple(ebuilds)

    @property
    def desc(self):
        return "invalid package name%s: [ %s ]" % (
            _pl(self.ebuilds), ', '.join(self.ebuilds))


class EqualVersions(results.PackageResult, results.Error):
    """Ebuilds that have equal versions.

    For example, cat/pn-1.0.2, cat/pn-1.0.2-r0, cat/pn-1.0.2-r00 and
    cat/pn-1.000.2 all have equal versions according to PMS and therefore
    shouldn't exist in the same repository.
    """

    def __init__(self, versions, **kwargs):
        super().__init__(**kwargs)
        self.versions = tuple(versions)

    @property
    def desc(self):
        return f"equal package versions: [ {', '.join(self.versions)} ]"


class DuplicateFiles(results.PackageResult, results.Warning):
    """Two or more identical files in FILESDIR."""

    def __init__(self, files, **kwargs):
        super().__init__(**kwargs)
        self.files = tuple(files)

    @property
    def desc(self):
        return 'duplicate identical files in FILESDIR: %s' % (
            ', '.join(map(repr, self.files)))


class EmptyFile(results.PackageResult, results.Warning):
    """File in FILESDIR is empty."""

    def __init__(self, filename, **kwargs):
        super().__init__(**kwargs)
        self.filename = filename

    @property
    def desc(self):
        return f'empty file in FILESDIR: {self.filename!r}'


class ExecutableFile(results.PackageResult, results.Warning):
    """File has executable bit, but doesn't need it."""

    def __init__(self, filename, **kwargs):
        super().__init__(**kwargs)
        self.filename = filename

    @property
    def desc(self):
        return f'unnecessary executable bit: {self.filename!r}'


class UnknownPkgDirEntry(results.PackageResult, results.Warning):
    """Unknown files or directories in package directory.

    Relevant for the gentoo repo only since the spec states that a package
    directory may contain other files or directories.
    """

    def __init__(self, filenames, **kwargs):
        super().__init__(**kwargs)
        self.filenames = tuple(filenames)

    @property
    def desc(self):
        files = ', '.join(map(repr, self.filenames))
        y = _pl(self.filenames, singular='y', plural='ies')
        return f'unknown entr{y}: {files}'


class SizeViolation(results.PackageResult, results.Warning):
    """File in $FILESDIR is too large (current limit is 20k)."""

    def __init__(self, filename, size, **kwargs):
        super().__init__(**kwargs)
        self.filename = filename
        self.size = size

    @property
    def desc(self):
        return f'{self.filename!r} exceeds 20k in size; {sizeof_fmt(self.size)} total'


class BannedCharacter(results.PackageResult, results.Error):
    """File or directory name doesn't abide by GLEP 31 requirements.

    See the official GLEP 31 documentation [#]_ for details.

    .. [#] https://www.gentoo.org/glep/glep-0031.html
    """

    def __init__(self, filename, chars, **kwargs):
        super().__init__(**kwargs)
        self.filename = filename
        self.chars = tuple(chars)

    @property
    def desc(self):
        chars = ', '.join(map(repr, self.chars))
        return (
            f'filename {self.filename!r} character{_pl(self.chars)} '
            f'outside allowed set: {chars}'
        )


class InvalidUTF8(results.PackageResult, results.Error):
    """File isn't UTF-8 compliant."""

    def __init__(self, filename, err, **kwargs):
        super().__init__(**kwargs)
        self.filename = filename
        self.err = err

    @property
    def desc(self):
        return f"invalid UTF-8: {self.err}: {self.filename!r}"


class PkgDirCheck(Check):
    """Scan ebuild directory for various file-related issues."""

    scope = base.package_scope
    _source = (sources.PackageRepoSource, (), (('source', sources.RawRepoSource),))

    ignore_dirs = frozenset(["cvs", ".svn", ".bzr"])
    required_addons = (git.GitAddon,)
    known_results = frozenset([
        DuplicateFiles, EmptyFile, ExecutableFile, UnknownPkgDirEntry, SizeViolation,
        BannedCharacter, InvalidUTF8, MismatchedPN, InvalidPN,
    ])

    # TODO: put some 'preferred algorithms by purpose' into snakeoil?
    digest_algo = 'sha256'

    def __init__(self, *args, git_addon):
        super().__init__(*args)
        self.gitignored = git_addon.gitignored

    def feed(self, pkgset):
        pkg = pkgset[0]
        pkg_path = pjoin(self.options.target_repo.location, pkg.category, pkg.package)
        ebuild_ext = '.ebuild'
        mismatched = []
        invalid = []
        unknown = []
        # note we don't use os.walk, we need size info also
        for filename in listdir(pkg_path):
            path = pjoin(pkg_path, filename)

            if self.gitignored(path):
                continue

            if os.path.isfile(path) and  os.stat(path).st_mode & 0o111:
                yield ExecutableFile(filename, pkg=pkg)

            # While this may seem odd, written this way such that the filtering
            # happens all in the genexp. If the result was being handed to any,
            # it's a frame switch each char, which adds up.
            banned_chars = set(filename) - allowed_filename_chars_set
            if banned_chars:
                yield BannedCharacter(filename, sorted(banned_chars), pkg=pkg)

            if filename.endswith(ebuild_ext):
                try:
                    with open(path, mode='rb') as f:
                        f.read(8192).decode()
                except UnicodeDecodeError as e:
                    yield InvalidUTF8(filename, str(e), pkg=pkg)

                pkg_name = os.path.basename(filename[:-len(ebuild_ext)])
                try:
                    pkg_atom = atom_cls(f'={pkg.category}/{pkg_name}')
                    if pkg_atom.package != os.path.basename(pkg_path):
                        mismatched.append(pkg_name)
                except MalformedAtom:
                    invalid.append(pkg_name)
            elif (self.options.gentoo_repo and
                    filename not in ('Manifest', 'metadata.xml', 'files')):
                unknown.append(filename)

        if mismatched:
            yield MismatchedPN(sorted(mismatched), pkg=pkg)
        if invalid:
            yield InvalidPN(sorted(invalid), pkg=pkg)
        if unknown:
            yield UnknownPkgDirEntry(sorted(unknown), pkg=pkg)

        files_by_size = defaultdict(list)
        pkg_path_len = len(pkg_path) + 1
        for root, dirs, files in os.walk(pjoin(pkg_path, 'files')):
            # don't visit any ignored directories
            for d in self.ignore_dirs.intersection(dirs):
                dirs.remove(d)
            base_dir = root[pkg_path_len:]
            for filename in files:
                path = pjoin(root, filename)
                if self.gitignored(path):
                    continue
                file_stat = os.lstat(path)
                if stat.S_ISREG(file_stat.st_mode):
                    if file_stat.st_mode & 0o111:
                        yield ExecutableFile(pjoin(base_dir, filename), pkg=pkg)
                    if file_stat.st_size == 0:
                        yield EmptyFile(pjoin(base_dir, filename), pkg=pkg)
                    else:
                        files_by_size[file_stat.st_size].append(pjoin(base_dir, filename))
                        if file_stat.st_size > 20480:
                            yield SizeViolation(
                                pjoin(base_dir, filename), file_stat.st_size, pkg=pkg)
                    banned_chars = set(filename) - allowed_filename_chars_set
                    if banned_chars:
                        yield BannedCharacter(
                            pjoin(base_dir, filename), sorted(banned_chars), pkg=pkg)

        files_by_digest = defaultdict(list)
        for size, files in files_by_size.items():
            if len(files) > 1:
                for f in files:
                    digest = get_chksums(pjoin(pkg_path, f), self.digest_algo)[0]
                    files_by_digest[digest].append(f)

        for digest, files in files_by_digest.items():
            if len(files) > 1:
                yield DuplicateFiles(sorted(files), pkg=pkg)


class EqualVersionsCheck(Check):
    """Scan package ebuilds for semantically equal versions."""

    scope = base.package_scope
    _source = sources.PackageRepoSource
    known_results = frozenset([EqualVersions])

    def feed(self, pkgset):
        equal_versions = defaultdict(set)
        sorted_pkgset = sorted(pkgset)
        for i, pkg_a in enumerate(sorted_pkgset):
            try:
                pkg_b = sorted_pkgset[i + 1]
            except IndexError:
                break
            if pkg_a.versioned_atom == pkg_b.versioned_atom:
                equal_versions[pkg_a.versioned_atom].update([pkg_a.fullver, pkg_b.fullver])
        for pkg, versions in equal_versions.items():
            yield EqualVersions(sorted(versions), pkg=pkg)
