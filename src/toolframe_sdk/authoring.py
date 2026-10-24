"""Optional authoring helpers; never imported by runtime definitions.

Generators use ``generate(*, destination: Path, parameters: dict)``. The caller
provides a new staging directory; helpers preserve template license bytes.
Install tomlkit in the distribution's ``authoring`` extra to use render_plugin.
"""

import ast
import io
import json
import re
import tokenize
from pathlib import Path


def plugin_names(identifier):
    if not isinstance(identifier, str) or not re.fullmatch(
        r"[a-z][a-z0-9-]{0,63}", identifier
    ):
        raise ValueError("Plugin identifier must match [a-z][a-z0-9-]{0,63}")
    package = f"toolframe-{identifier}"
    if package.endswith("-"):
        package += "plugin"
    return package, package.replace("-", "_")


def write_template(destination, rendered):
    """Write only new regular files beneath an existing empty staging directory."""
    for relative, content in rendered.items():
        relative = Path(relative)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Template file escapes its destination")
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as handle:
            handle.write(content)


def _files(directory, prefix=Path()):
    # Traversable also works when core's resources are not ordinary source files.
    for child in sorted(directory.iterdir(), key=lambda item: item.name):
        relative = prefix / child.name
        if child.is_dir():
            yield from _files(child, relative)
        else:
            yield relative, child.read_bytes()


def _python(source, rename):
    tokens = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.STRING:
            value = ast.literal_eval(token.string)
            if isinstance(value, str) and (replacement := rename(value)) != value:
                token = token._replace(string=json.dumps(replacement))
        elif token.type == tokenize.NAME:
            token = token._replace(string=rename(token.string))
        tokens.append(token)
    return tokenize.untokenize(tokens)


def render_plugin(directory, identifier, *, text_suffixes=()):
    """Render a single src-layout plugin template, changing identity tokens only."""
    package, module = plugin_names(identifier)
    import tomlkit

    template = dict(_files(directory))
    document = tomlkit.parse(template[Path("pyproject.toml")].decode("utf-8"))
    project = document["project"]
    entries = project["entry-points"]["toolframe.plugins"]
    if len(entries) != 1:
        raise ValueError("Bundled template must declare exactly one plugin entry point")
    original_id, entry = next(iter(entries.items()))
    original_module = entry.split(".", 1)[0]
    wheel = document["tool"]["hatch"]["build"]["targets"]["wheel"]
    if wheel["packages"] != [f"src/{original_module}"]:
        raise ValueError("Bundled template must declare one src-layout Python package")

    identities = {
        original_id: identifier,
        project["name"]: package,
        original_module: module,
    }
    prefixes = {
        f"{original_id}.": f"{identifier}.",  # Service namespace.
        f"{original_id}-": f"{identifier}-",  # Streamlit widget keys.
        f"{original_id}__": f"{identifier}__",  # Qualified MCP tool names.
        f"{original_module}.": f"{module}.",  # Imports and module:factory references.
    }

    def rename(value):
        if value in identities:
            return identities[value]
        for before, after in prefixes.items():
            if value.startswith(before):
                return after + value[len(before) :]
        return value

    project["name"] = package
    project["entry-points"]["toolframe.plugins"] = {identifier: rename(entry)}
    wheel["packages"] = [f"src/{module}"]
    template[Path("pyproject.toml")] = tomlkit.dumps(document).encode("utf-8")

    rendered = {}
    for relative, content in template.items():
        if relative.parts[:2] == ("src", original_module):
            relative = Path("src", module, *relative.parts[2:])
        if relative.suffix == ".py":
            source = _python(content.decode("utf-8"), rename)
            compile(source, str(relative), "exec")
            content = source.encode("utf-8")
        elif relative.suffix == ".md":
            # Only code spans name callable tools/packages; prose stays verbatim.
            source = re.sub(
                r"`([^`\n]+)`",
                lambda match: f"`{rename(match[1])}`",
                content.decode("utf-8"),
            )
            content = source.encode("utf-8")
        elif relative.suffix in text_suffixes:
            content = (
                content.decode("utf-8")
                .replace(original_module, module)
                .replace(original_id, identifier)
                .encode("utf-8")
            )
        if ".md" in text_suffixes and relative.suffix == ".md":
            content = (
                content.decode("utf-8")
                .replace(original_module, module)
                .replace(original_id, identifier)
                .encode("utf-8")
            )
        rendered[relative] = content
    return rendered
