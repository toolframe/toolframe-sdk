# Optional template authoring helpers

Runtime plugins use the normal SDK contracts. Template generators can optionally
use `toolframe_sdk.authoring` to render a packaged src-layout example into a new
project:

```python
from importlib.resources import files
from toolframe_sdk.authoring import render_plugin, write_template


def generate(*, destination, parameters):
    rendered = render_plugin(
        files("my_authoring_package").joinpath("templates/example"),
        parameters["name"],
    )
    write_template(destination, rendered)
```

The template has one `toolframe.plugins` entry and one Hatch wheel package in its
own packaged `pyproject.toml`. This is template data, not an assumption that an
installed distribution retains its original source pyproject. Identity tokens in
Python code and Markdown code spans are renamed while prose and license bytes are
preserved. `text_suffixes` explicitly opts other text assets into replacement.
`plugin_names` returns the distribution and Python module names for a plugin ID.

`render_plugin` lazily imports `tomlkit`; a generator distribution can put it in an
optional `authoring` extra. Runtime-only SDK installation does not require it.
`write_template` writes new files only and rejects paths outside its destination.
Core handles exclusive final-directory publication and generator environment
selection. These helpers do not register or activate a runtime plugin.

Generator distributions publish static authoring metadata as described in
[Core's authoring protocol](https://github.com/toolframe/toolframe-core#readme).
A generator can instead implement the protocol directly without these helpers or
an SDK dependency. Provider-specific templates belong to their own distributions.
