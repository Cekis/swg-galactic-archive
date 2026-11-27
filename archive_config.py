"""Resolve the source repositories and workspace for an Archive configuration."""

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
from tempfile import TemporaryDirectory


PROJECT = Path(__file__).resolve().parent
DEFAULT_CONFIG = PROJECT / ".local/sources.json"
ROLES = ("src", "dsrc", "clientdata", "client-tools", "game-data")
REQUIRED = ("dsrc", "clientdata")
GIT_SHA = re.compile(r"[0-9a-f]{40}")
GITHUB_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]+")


def github_repository(value):
    if (not isinstance(value, str) or not GITHUB_REPOSITORY.fullmatch(value)
            or value.split("/")[1] in {".", ".."} or value.endswith(".git")):
        raise ValueError("GitHub repositories must be specified as OWNER/REPOSITORY.")
    return value


def relative_directory(value):
    if (not isinstance(value, str) or any(piece in ("", ".", "..") for piece in value.split("/"))
            or re.search(r"[:\\\x00-\x1f\x7f]", value)):
        raise ValueError("Repository subdirectories must be relative paths with forward slashes.")
    return value


def git_output(checkout, *args, optional=False):
    result = subprocess.run(["git", *args], cwd=checkout, capture_output=True)
    if result.returncode:
        if optional:
            return None
        message = result.stderr.decode("utf-8", errors="replace").strip()
        directory = f" in {checkout}" if checkout is not None else ""
        detail = f": {message}" if message else ""
        raise ValueError(f"Git {args[0]} failed{directory} (exit {result.returncode}){detail}")
    return result.stdout


def commit(checkout, revision="HEAD"):
    return git_output(checkout, "rev-parse", "--verify", "--end-of-options", revision + "^{commit}").decode().strip()


def github_revision(slug, selector):
    if selector and GIT_SHA.fullmatch(selector):
        return selector
    if selector in (None, "HEAD"):
        ref = "HEAD"
    else:
        ref = "refs/heads/" + selector.removeprefix("refs/heads/")
        if git_output(None, "check-ref-format", ref, optional=True) is None:
            raise ValueError(f"Invalid Git branch {selector!r} for {slug}.")
    patterns = ("HEAD",) if ref == "HEAD" else ("HEAD", ref)
    try:
        refs = git_output(None, "ls-remote", "--symref", f"https://github.com/{slug}.git", *patterns)
    except ValueError as error:
        raise ValueError(f"Unable to read branches for {slug}. {error}") from error

    default_branch = None
    for row in refs.decode("utf-8").splitlines():
        value, _, label = row.partition("\t")
        if label == ref and GIT_SHA.fullmatch(value):
            return value
        if label == "HEAD" and value.startswith("ref: refs/heads/"):
            default_branch = value.removeprefix("ref: refs/heads/")
    if ref == "HEAD":
        raise ValueError(f"{slug} has no default branch with a commit.")
    branch = ref.removeprefix("refs/heads/")
    message = f"{slug} has no branch named {branch!r}."
    if default_branch:
        message += f" Its default branch is {default_branch!r}. Omit revision to follow it."
    raise ValueError(message)


def github_checkout(slug, revision, cache):
    """Fetch a pinned snapshot once. Worktrees supplied by the user are read-only."""
    owner, name = slug.lower().split("/")
    target = cache / owner / name / revision
    url = f"https://github.com/{slug}.git"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="fetch-", dir=target.parent) as work:
            candidate = Path(work) / "checkout"
            candidate.mkdir()
            print(f"Fetching {slug} at {revision[:9]}", flush=True)
            git_output(candidate, "init", "--quiet")
            git_output(candidate, "remote", "add", "origin", url)
            git_output(candidate, "fetch", "--no-tags", "--depth=1", "origin", revision)
            git_output(candidate, "checkout", "--quiet", "--detach", "FETCH_HEAD")
            if commit(candidate) != revision:
                raise ValueError(f"GitHub selected an unexpected revision for {slug}.")
            try:
                candidate.rename(target)
            except OSError:
                if not target.is_dir():
                    raise
    if (commit(target) != revision
            or git_output(target, "config", "--local", "--get", "remote.origin.url").decode().strip().lower() != url.lower()):
        raise ValueError(f"Cached checkout does not match {slug} at {revision}: {target}")
    return target


@dataclass(frozen=True)
class SourceRepository:
    checkout: Path
    subdirectory: str = ""
    revision: str | None = None
    github: str | None = None

    @property
    def root(self):
        return self.checkout / self.subdirectory

    def repository_path(self, path):
        return (PurePosixPath(self.subdirectory) / path).as_posix()

    def tree(self, *prefixes):
        """Yield paths relative to the Git root and blob IDs from the selected commit."""
        if self.revision is None:
            return
        selected = [self.repository_path(path) for path in prefixes] if prefixes else [self.subdirectory]
        fields = git_output(self.checkout, "--literal-pathspecs", "ls-tree", "-r", "-z", self.revision,
                            "--", *(path for path in selected if path))
        for entry in fields.split(b"\0"):
            if not entry:
                continue
            info, path = entry.split(b"\t", 1)
            mode, kind, oid = info.split()
            if kind == b"blob" and mode.startswith(b"100"):
                yield path.decode("utf-8"), oid.decode("ascii")

    def blobs(self, entries):
        """Stream the selected committed blobs without materializing another checkout."""
        with subprocess.Popen(["git", "cat-file", "--batch"], cwd=self.checkout, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE) as child:
            try:
                for name, oid in entries:
                    key = oid.encode("ascii")
                    child.stdin.write(key + b"\n")
                    child.stdin.flush()
                    header = child.stdout.readline().split()
                    if len(header) != 3 or header[:2] != [key, b"blob"]:
                        raise ValueError(f"Cannot read pinned source {name} from {self.checkout}.")
                    size = int(header[2])
                    data = child.stdout.read(size)
                    if len(data) != size or child.stdout.read(1) != b"\n":
                        raise ValueError(f"Incomplete pinned source {name} from {self.checkout}.")
                    yield name, data
            finally:
                child.stdin.close()
                child.stdout.close()
            if child.wait():
                raise ValueError(f"Git source export failed in {self.checkout}.")

    def read(self, path):
        if self.revision:
            return git_output(self.checkout, "cat-file", "blob",
                              f"{self.revision}:{self.repository_path(path)}", optional=True)
        candidate = (self.root / path).resolve()
        try:
            if candidate.is_relative_to(self.root.resolve()) and candidate.is_file():
                return candidate.read_bytes()
        except OSError:
            pass
        return None


@dataclass(frozen=True)
class ArchiveProfile:
    repositories: dict
    state: Path
    key: str | None = None
    quest_annotations: bytes | None = None
    legacy_game: Path | None = None

    @property
    def database(self):
        return self.state / "catalog.sqlite3"

    @property
    def overlay(self):
        return self.state / "model-assets" / self.key if self.key else self.state / "model-assets"

    def root(self, role):
        repo = self.repositories.get(role)
        return repo.root if repo else self.state / "unused" / role

    def metadata(self):
        return {role: {"revision": repo.revision, "github": repo.github,
                       "subdirectory": repo.subdirectory} for role, repo in self.repositories.items()}


def configured_repository(role, entry, parent, cache, selections):
    if not isinstance(entry, dict) or set(entry) - {"path", "github", "revision", "subdirectory"}:
        raise ValueError(f"Invalid configuration for source {role}.")
    path, slug, revision = (entry.get(key) for key in ("path", "github", "revision"))
    if (path is None) == (slug is None):
        raise ValueError(f"Set exactly one of path or github for {role}.")
    subdirectory = relative_directory(entry["subdirectory"]) if "subdirectory" in entry else ""
    if revision is not None and (not isinstance(revision, str) or not revision.strip()):
        raise ValueError(f"Invalid Git revision for {role}.")
    if slug is not None:
        github_repository(slug)
        selection = slug.lower(), revision
        if selection not in selections:
            selections[selection] = github_revision(slug, revision)
        revision = selections[selection]
        repo = SourceRepository(github_checkout(slug, revision, cache), subdirectory, revision, slug)
    else:
        if not isinstance(path, str) or not path.strip():
            raise ValueError(f"Invalid checkout path for {role}.")
        checkout = (parent / Path(path).expanduser()).resolve()
        if not checkout.is_dir():
            raise ValueError(f"Source directory for {role} does not exist: {checkout}")
        git_root = git_output(checkout, "rev-parse", "--show-toplevel", optional=True)
        if git_root:
            top = Path(git_root.decode("utf-8").strip()).resolve()
            subdirectory = (checkout.relative_to(top) / subdirectory).as_posix().removeprefix("./")
            subdirectory = "" if subdirectory == "." else subdirectory
            selected = commit(top, revision or "HEAD")
            if revision and role not in ("dsrc", "src") and selected != commit(top):
                raise ValueError(f"Check out the selected revision of {role} or use its GitHub source.")
            repo = SourceRepository(top, subdirectory, selected)
        else:
            if revision or role in ("dsrc", "src"):
                raise ValueError(f"{role} must be a Git checkout to read a pinned source revision.")
            repo = SourceRepository(checkout, subdirectory)
    if not repo.root.is_dir() and (role not in ("dsrc", "src") or not any(repo.tree())):
        raise ValueError(f"Source subdirectory for {role} does not exist: {repo.root}")
    return repo


def load_profile(path, project=PROJECT):
    path = path.resolve()
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict) or set(config) - {"sources", "quest_annotations"}:
        raise ValueError("Archive source configuration accepts sources and optional quest_annotations.")
    values = config.get("sources")
    if not isinstance(values, dict) or set(values) - set(ROLES) or any(values.get(role) is None for role in REQUIRED):
        raise ValueError("Select dsrc and clientdata. Optional sources are src, client-tools and game-data.")
    local = project / ".local"
    selections = {}
    repos = {role: configured_repository(role, values[role], path.parent, local / "repositories", selections)
             for role in ROLES if values.get(role) is not None}
    annotation_file = config.get("quest_annotations")
    annotations = None
    if annotation_file is not None:
        if not isinstance(annotation_file, str) or not annotation_file.strip():
            raise ValueError("quest_annotations must be a file path relative to the configuration or an absolute path.")
        annotations = (path.parent / Path(annotation_file).expanduser()).read_bytes()
    identity = {role: (str(repo.root), repo.revision, repo.github) for role, repo in repos.items()}
    fingerprint = sha256(json.dumps(identity, sort_keys=True).encode() + (annotations or b"")).hexdigest()
    profile_directory = sha256(str(path).encode()).hexdigest()[:20]
    return ArchiveProfile(repos, local / "profiles" / profile_directory, fingerprint, annotations)


def legacy_game(project=PROJECT):
    if value := os.environ.get("SWG_GAME_ROOT"):
        return Path(value).expanduser().resolve()
    for candidate in (project / "game", project.parent / "whitengold-ai" / "whitengold", project.parent):
        if (candidate / "docs/quests/manifest.json").is_file():
            return candidate.resolve()
    return project / "game"


def legacy_asset(directory, environment, project=PROJECT):
    if value := os.environ.get(environment):
        return Path(value).expanduser().resolve()
    game = legacy_game(project)
    parents = (project,) if game == project / "game" else (project, project.parent, *game.parents)
    return next((parent / directory for parent in parents if (parent / directory).is_dir()), project / directory)


def add_source_options(parser):
    parser.add_argument("--sources", type=Path, help="Source configuration (defaults to .local/sources.json).")
    parser.add_argument("--database", type=Path, help="Override the selected configuration's catalog file.")
    parser.add_argument("--clientdata", type=Path, help="Extracted client assets for legacy catalogs.")
    parser.add_argument("--client-tools", type=Path, help="Client tools for legacy catalogs.")


def select_profile(args, *, packaged=False):
    if packaged:
        if args.sources or args.clientdata or args.client_tools:
            raise ValueError("Extracted deployments use the sources and assets recorded in their package.")
        return ArchiveProfile({role: SourceRepository(path) for role, path in {
            "clientdata": PROJECT / "clientdata", "client-tools": PROJECT / "client-tools",
            "game-data": PROJECT / "game/data",
        }.items()}, PROJECT / ".local")
    if path := args.sources or (DEFAULT_CONFIG if DEFAULT_CONFIG.is_file() else None):
        if args.clientdata or args.client_tools:
            raise ValueError("Set the client inputs in the source configuration when using --sources.")
        return load_profile(path)
    game = legacy_game()
    clients = {
        "clientdata": args.clientdata or legacy_asset("clientdata", "SWG_CLIENTDATA"),
        "client-tools": args.client_tools or legacy_asset("client-tools", "SWG_CLIENT_TOOLS"),
        "game-data": game / "data",
    }
    return ArchiveProfile({role: SourceRepository(path.resolve()) for role, path in clients.items()},
                          PROJECT / ".local", legacy_game=game)
