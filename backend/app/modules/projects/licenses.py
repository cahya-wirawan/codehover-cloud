"""Which licenses let a workspace publish a public site of someone else's repository (01 FR-029, 21).

A project read through the `public` connection (FR-028) is a repository the workspace does not hold an App
grant for, so a public site of it redistributes another party's code, together with explanations derived
from it. That is allowed only when GitHub detects, at the exact commit of the build, a license that
permits redistributing the code verbatim and modified: the open-source licenses below. Everything else is
refused: no license file (all rights reserved), a file GitHub cannot name (`NOASSERTION`), and licenses
that restrict use or redistribution. The license file itself must be in the build's snapshot, so the site
carries the notice these licenses require, and the site names the license beside the repository.

Private sites (members only), App-granted repositories and uploaded archives are not checked: the
workspace either shares nothing outside itself or supplied the code itself.
"""

from __future__ import annotations

# SPDX identifiers as GitHub reports them (its licensee list). Licenses for fonts, data or documentation
# (OFL, ODbL, GFDL) are left out: they are not meant for source code.
PERMITTED = frozenset(
    {
        "0BSD",
        "AFL-3.0",
        "AGPL-3.0",
        "Apache-2.0",
        "Artistic-2.0",
        "BlueOak-1.0.0",
        "BSD-2-Clause",
        "BSD-2-Clause-Patent",
        "BSD-3-Clause",
        "BSD-3-Clause-Clear",
        "BSD-4-Clause",
        "BSL-1.0",
        "CC-BY-4.0",
        "CC-BY-SA-4.0",
        "CC0-1.0",
        "CECILL-2.1",
        "ECL-2.0",
        "EPL-1.0",
        "EPL-2.0",
        "EUPL-1.1",
        "EUPL-1.2",
        "GPL-2.0",
        "GPL-3.0",
        "ISC",
        "LGPL-2.1",
        "LGPL-3.0",
        "LPPL-1.3c",
        "MIT",
        "MIT-0",
        "MPL-2.0",
        "MS-PL",
        "MS-RL",
        "MulanPSL-2.0",
        "NCSA",
        "OSL-3.0",
        "PostgreSQL",
        "UPL-1.0",
        "Unlicense",
        "Vim",
        "WTFPL",
        "Zlib",
    }
)


def permits_public_site(spdx_id: str | None) -> bool:
    return spdx_id in PERMITTED
