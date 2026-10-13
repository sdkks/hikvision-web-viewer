"""Pure logic for the camera's motion-detection zone map.

The camera stores motion zones as a row-major bit grid. `gridMap` in the ISAPI
document is 108 hex characters: 6 per row for 18 rows of 22 columns.

Cell order is fixed by the vendor, not guessed. Hikvision's ISAPI IPMD
(Intelligent Motion Detection) Service guide states: "The first cell is in the
upper left corner. Then the cell order goes first from left to right and then
from up to down." `1` = detect (armed), `0` = no detect (masked). So within each
row's 24-bit group the FIRST cell (leftmost, column 0) is the MSB and the two
low bits of every row are padding (e.g. ``fffffc`` = all 22 cells armed). The
vendor's own worked example is a test vector in tests/test_viewer.py.

Everything that depends on bit order or shape lives here so there is exactly one
source of truth. One residual assumption cannot be proven from the XML alone and
is named below:

  GRID_SPANS_FRAME  the 18x22 grid is assumed to stretch over the full streamed
                    frame rectangle, which is how the browser overlay is drawn
                    (see static/app.js). Not provable from the XML alone.

`True` means the cell is ARMED (motion here triggers); `False` means masked
(ignored). "Masked" is simply the complement of armed - the camera stores one
bit per cell, so the mask is not a separate value.
"""

ROWS = 18
COLS = 22
HEX_PER_ROW = 6  # 6 hex chars = 24 bits; 22 cells + 2 padding bits
GRID_HEX_LEN = ROWS * HEX_PER_ROW  # 108
BITS_PER_ROW = HEX_PER_ROW * 4  # 24
HEX_DIGITS = set("0123456789abcdefABCDEF")

# ── documented facts and assumptions (see module docstring) ──────────────────
# ROW_ZERO_IS_TOP: vendor-documented. The Hikvision IPMD guide fixes the first
# cell as the upper-left and orders cells left-to-right then top-to-bottom, so
# row 0 is the top of the image. Kept named so a flipped-mount sketch stays
# testable without touching decode/encode.
ROW_ZERO_IS_TOP = True
# GRID_SPANS_FRAME: residual assumption - the 18x22 grid spans the full streamed
# frame rectangle (matches the browser overlay in static/app.js).
GRID_SPANS_FRAME = True

# The camera's capabilities endpoint reports sensitivityLevel min=0 max=100
# step=20, so only these six values are accepted (others are silently ignored).
SENSITIVITY_STEPS = (0, 20, 40, 60, 80, 100)

# Coarse "quick zone" layer: exact whole-cell rectangles, 2 columns x 6 rows.
ZONE_COLS = 2
ZONE_ROWS = 6
ZONE_CELL_W = COLS // ZONE_COLS  # 11
ZONE_CELL_H = ROWS // ZONE_ROWS  # 3


def validate_gridmap(grid_map: str) -> str:
    """Return the gridMap unchanged, or raise ValueError if the camera sent a shape
    we cannot safely decode.

    Firmware drift guard: exactly 108 hex characters, no separators or whitespace.
    """
    if not isinstance(grid_map, str):
        raise ValueError("gridMap is not a string")
    if len(grid_map) != GRID_HEX_LEN:
        raise ValueError(f"gridMap must be {GRID_HEX_LEN} hex characters, got {len(grid_map)}")
    bad = set(grid_map) - HEX_DIGITS
    if bad:
        raise ValueError(f"gridMap has non-hex characters: {''.join(sorted(bad))}")
    return grid_map


def decode_gridmap(grid_map: str) -> list[list[bool]]:
    """108 hex chars -> rows[18][22] of booleans (True = armed)."""
    validate_gridmap(grid_map)
    rows: list[list[bool]] = []
    for r in range(ROWS):
        row_value = int(grid_map[r * HEX_PER_ROW : (r + 1) * HEX_PER_ROW], 16)
        rows.append([bool((row_value >> (BITS_PER_ROW - 1 - c)) & 1) for c in range(COLS)])
    return rows


def encode_gridmap(rows: list[list[bool]], base_grid_map: str | None = None) -> str:
    """rows[18][22] -> 108 hex chars.

    The two low bits of each row are padding, not cells. When `base_grid_map` is
    given (the camera document the edit started from) those padding bits keep the
    values the camera returned; otherwise they are zero.
    """
    if len(rows) != ROWS or any(len(row) != COLS for row in rows):
        raise ValueError(f"rows must be {ROWS}x{COLS}")
    if base_grid_map is not None:
        validate_gridmap(base_grid_map)
    out: list[str] = []
    for r, row in enumerate(rows):
        row_value = 0
        for c, armed in enumerate(row):
            if armed:
                row_value |= 1 << (BITS_PER_ROW - 1 - c)
        if base_grid_map is not None:
            start = r * HEX_PER_ROW
            padding = int(base_grid_map[start : start + HEX_PER_ROW], 16) & 0b11
            row_value |= padding
        out.append(f"{row_value:0{HEX_PER_ROW}x}")
    return "".join(out)


def empty_grid() -> list[list[bool]]:
    return [[False] * COLS for _ in range(ROWS)]


def clone(rows: list[list[bool]]) -> list[list[bool]]:
    return [list(row) for row in rows]


def set_region(
    rows: list[list[bool]], r0: int, c0: int, r1: int, c1: int, arm: bool
) -> list[list[bool]]:
    """Set a half-open rectangle (rows r0..r1-1, cols c0..c1-1) to armed/masked.

    Mutates and returns `rows` (the grid is 18x22; copying on every brush dab is
    pointless churn).
    """
    for r in range(max(0, r0), min(ROWS, r1)):
        for c in range(max(0, c0), min(COLS, c1)):
            rows[r][c] = arm
    return rows


def zone_bounds(zone_row: int, zone_col: int) -> tuple[int, int, int, int]:
    """Half-open (r0, c0, r1, c1) for one whole-cell quick zone."""
    if not (0 <= zone_row < ZONE_ROWS and 0 <= zone_col < ZONE_COLS):
        raise ValueError("zone out of range")
    r0, c0 = zone_row * ZONE_CELL_H, zone_col * ZONE_CELL_W
    return r0, c0, r0 + ZONE_CELL_H, c0 + ZONE_CELL_W


def apply_tool_to_zone(
    rows: list[list[bool]], zone_row: int, zone_col: int, arm: bool
) -> list[list[bool]]:
    """Apply the arm/mask tool to every cell of a quick zone (mutates, returns)."""
    return set_region(rows, *zone_bounds(zone_row, zone_col), arm)


def invert(rows: list[list[bool]]) -> list[list[bool]]:
    for r in range(ROWS):
        for c in range(COLS):
            rows[r][c] = not rows[r][c]
    return rows


def counts(rows: list[list[bool]]) -> dict:
    armed = sum(1 for row in rows for cell in row if cell)
    total = ROWS * COLS
    return {"armed": armed, "masked": total - armed, "total": total}


def summary(counted: dict) -> str:
    """Human sentence for the live region, e.g. '342 of 396 cells armed (86%)'."""
    total = counted.get("total") or (ROWS * COLS)
    armed = counted.get("armed", 0)
    pct = round(100 * armed / total) if total else 0
    return f"{armed} of {total} cells armed ({pct}%)"


def valid_sensitivity(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value in SENSITIVITY_STEPS


def diff(intended: dict, actual: dict) -> list[str]:
    """Human-readable differences between the requested and read-back state.

    The UI reports the readback, never the request; this is what makes drift
    (camera clamping/normalising) visible instead of silently assumed away.
    """
    out: list[str] = []
    if bool(intended.get("enabled")) != bool(actual.get("enabled")):
        out.append(f"The camera kept motion detection {'on' if actual.get('enabled') else 'off'}.")
    if bool(intended.get("enable_highlight")) != bool(actual.get("enable_highlight")):
        out.append(
            "The camera kept the live highlight "
            f"{'on' if actual.get('enable_highlight') else 'off'}."
        )
    if intended.get("sensitivity") != actual.get("sensitivity"):
        out.append(f"The camera kept sensitivity at {actual.get('sensitivity')}.")
    if intended.get("cells") != actual.get("cells"):
        ac = counts(actual.get("cells") or empty_grid())
        out.append(f"The camera stored {ac['armed']} of {ac['total']} cells armed.")
    return out
