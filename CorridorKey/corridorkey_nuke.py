"""CorridorKey 2.0 for Nuke.

Creates a "CorridorKey" group node. Press "Run CorridorKey" on it: the plate
(and optional hint matte) is rendered to an EXR cache on disk, the
CorridorKey 2.0 model runs in its own Python environment in the background,
and the keyed result is read back into the node.

Colour: the model was trained on ordinary display-referred (sRGB / Rec.709
looking) footage. The plate is converted from Nuke's working space (ACEScg in
ACES configs, linear in nuke-default) to a display-referred OCIO role before
caching, and the keyed foreground is converted back into the working space, so
the node behaves the same in ACES and non-ACES scripts.

The CorridorKey 2.0 model and runtime ("the backend") are installed separately
(see install/install_backend.py). The plugin finds the backend through, in order:
the CORRIDORKEY_BACKEND environment variable, "backend" in corridorkey_config.json
next to this file, or ~/CorridorKey2.0.

CorridorKey 2.0 is by Corridor Digital / Zachary Harris:
https://github.com/nikopueringer/CorridorKey2.0 - this Nuke plugin builds on it and
is released under the same Corridor Key Licence (see LICENSE).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import nuke

__version__ = "1.0.0"

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(PLUGIN_DIR, "corridorkey_config.json")


def _config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def backend_dir() -> str:
    """Folder of the CorridorKey 2.0 checkout (contains infer.py and venv/)."""
    candidates = [os.environ.get("CORRIDORKEY_BACKEND", ""), _config().get("backend", ""),
                  os.path.expanduser("~/CorridorKey2.0")]
    for c in candidates:
        if c and os.path.isfile(os.path.join(c, "infer.py")):
            return os.path.normpath(c)
    return os.path.normpath(next((c for c in candidates if c), candidates[-1]))


def _conda_env_dirs(backend: str) -> list:
    """Where install_backend.py puts the conda env for this backend (path without / with spaces)."""
    if sys.platform == "win32":
        drive_root = os.path.splitdrive(os.path.abspath(backend))[0] + "\\"
        roots = [os.path.join(backend, "_conda"), os.path.join(drive_root, "CorridorKey_conda")]
    else:
        roots = [os.path.join(backend, "_conda"), os.path.expanduser("~/.corridorkey_conda")]
    return [os.path.join(r, "envs", "corridorkey") for r in roots]


def venv_python() -> str:
    """Python that runs CorridorKey: config > the installer's conda env > an older venv install."""
    cfg = _config().get("python", "")
    if cfg and os.path.isfile(cfg):
        return cfg
    win = sys.platform == "win32"
    exe = "python.exe" if win else os.path.join("bin", "python")
    backend = backend_dir()
    candidates = [os.path.join(d, exe) for d in _conda_env_dirs(backend)]
    candidates.append(os.path.join(backend, "venv", *(("Scripts", "python.exe") if win else ("bin", "python"))))
    for p in candidates:
        if os.path.isfile(p):
            return p
    return cfg or candidates[0]


def _python_dirs() -> list:
    """Folders to put on PATH so the env's python and its DLLs work without 'conda activate'."""
    root = os.path.dirname(venv_python())
    if sys.platform != "win32":
        return [root]
    if os.path.basename(root).lower() == "scripts":        # venv: <venv>/Scripts/python.exe
        return [root]
    return [root] + [os.path.join(root, *p) for p in (("Library", "mingw-w64", "bin"), ("Library", "usr", "bin"),
                                                      ("Library", "bin"), ("Scripts",))]


def set_backend(path: str) -> None:
    cfg = _config()
    cfg["backend"] = os.path.normpath(path).replace("\\", "/")
    cfg.pop("python", None)      # found again for the new backend (conda env or venv)
    cfg.pop("ffmpeg", None)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=1)


def choose_backend():
    """Menu: pick the CorridorKey 2.0 backend folder (the one that contains infer.py)."""
    path = nuke.getFilename("Select the CorridorKey2.0 folder (contains infer.py)")
    if not path:
        return
    path = path.rstrip("/\\ ")
    if os.path.isfile(path):
        path = os.path.dirname(path)
    if not os.path.isfile(os.path.join(path, "infer.py")):
        nuke.message(f"infer.py not found in:\n{path}\n\nPick the CorridorKey2.0 folder made by the installer.")
        return
    set_backend(path)
    ok = os.path.isfile(venv_python())
    nuke.message(f"CorridorKey backend set to:\n{path}\n\n"
                 + ("Python environment found." if ok else f"Warning: no Python environment at\n{venv_python()}"))

HINT_MODES = ["none (model picks subject)", "first frame only", "every frame"]
HINT_CHANNELS = ["alpha", "red"]
OUTPUTS = ["premultiplied FG", "straight FG", "matte only", "plate + alpha"]
MEMORY = ["auto", "low VRAM"]
# what the model sees -> OCIO role (exists in ACES configs and in nuke-default)
MODEL_COLOURS = ["viewer look (recommended)", "sRGB texture (no tone map)"]
MODEL_ROLES = ["color_picking", "matte_paint"]
# OCIO v1 (Nuke 12/13) and v2 (Nuke 17) configs don't always define the same role names.
WORKING_SPACE_CANDIDATES = ["scene_linear", "linear", "Linear"]


def _set_colourspace(node, knob_name, candidates):
    """Set an OCIOColorSpace knob to the first candidate the current OCIO config knows.
    Warns (instead of failing silently) when none of them exists, so a wrong config is visible."""
    knob = node[knob_name]
    try:
        known = list(knob.values())
    except Exception:
        known = []
    for name in candidates:
        if not known or name in known:
            knob.setValue(name)
            return name
    knob.setValue(candidates[0])
    nuke.tprint(f"[CorridorKey] {node.name()}.{knob_name}: none of {candidates} is in this OCIO config; "
                f"check the colour config (ACES / nuke-default)")
    return candidates[0]

KEY_FILE = "[value parent.cache_dir]/out/plate_cutout_exr/cutout_######.exr"

HELP_HTML = """<b>CorridorKey 2.0 - how to use</b><br><br>
<b>1. Plate</b> - connect your green/blue screen plate to the <i>Plate</i> input.<br>
Works in ACES and in Nuke's default colour: the plate is converted to the display<br>
look the model was trained on, and the key comes back in your working space.<br><br>
<b>2. Cache folder</b> - where the plate frames and the key are saved.<br>
Use one folder per CorridorKey node. A copy-pasted node keeps the same folder;<br>
Run then offers it its own subfolder. <b>Required</b>: Run will not start without it.<br><br>
<b>3. Frame range</b> - <i>from input</i> = the plate's range, <i>in/out</i> = the Viewer's<br>
in and out points. The model keys the whole range together (that keeps it stable):<br>
press <b>Run CorridorKey</b> and watch the progress bar.<br><br>
<b>4. Hint</b> (optional 2nd input) - a matte that tells the model WHAT to keep:<br>
white = subject, black = remove. Use it when the model keeps stands, rigs or<br>
furniture, or picks the wrong subject. A rough, slightly loose Roto is enough.<br>
&nbsp;&nbsp;- <i>first frame only</i>: one matte on the first frame of the range;<br>
&nbsp;&nbsp;&nbsp;&nbsp;the model follows the subject through the shot.<br>
&nbsp;&nbsp;- <i>every frame</i>: an animated roto / garbage matte (most control).<br>
&nbsp;&nbsp;- <i>channel</i>: which channel of the hint input holds the matte.<br>
&nbsp;&nbsp;- <i>hint quality</i>: 0.95 clean roto, 0.75 rough matte, 0.5 loose garbage matte.<br><br>
<b>5. Output</b> - changes instantly, no re-run:<br>
premultiplied FG, straight FG, matte only, or plate + alpha<br>
(your original plate pixels with the new alpha - best for highlights/HDR).<br>
<i>key starts at</i>: frame where the key begins (set automatically); <i>reset</i> restores it.<br>
<i>clip black / clip white</i>: tighten the matte live (background noise / core holes).<br>
<i>despill</i> is baked into the key: re-run after changing it.<br><br>
<b>6. Advanced</b> - <i>model colour</i>: leave on viewer look.<br>
<i>GPU memory</i>: auto, or low VRAM if you get "CUDA out of memory".<br>
<i>window</i>: frames keyed together (0 = auto).<br><br>
<b>Create Read + Switch</b> (Ctrl+Alt+K) builds a Read of the keyed EXRs and an<br>
output Switch in your node graph - also done automatically after every run.
"""

_jobs: dict[str, "_Job"] = {}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _find_ffmpeg() -> str:
    """ffmpeg for CorridorKey: config > the installer's conda env > PATH > winget."""
    cfg = _config().get("ffmpeg", "")
    if cfg and os.path.isfile(cfg):
        return cfg
    for d in _python_dirs():
        for name in ("ffmpeg.exe", "ffmpeg"):
            if os.path.isfile(os.path.join(d, name)):
                return os.path.join(d, name)
    found = shutil.which("ffmpeg")
    if found:
        return found
    winget = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")
    if os.path.isdir(winget):
        for root, _dirs, files in os.walk(winget):
            if "ffmpeg.exe" in files:
                return os.path.join(root, "ffmpeg.exe")
    return "ffmpeg"


def _safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_") or "untitled"


CACHE_REQUIRED_MSG = ("Set a cache folder first.\n\n"
                      "CorridorKey saves the plate frames and the key there, so choose where on your PC "
                      "they should go: click the folder icon next to 'cache folder' (or type a path), then run again.\n\n"
                      "Use one folder per CorridorKey node.")


def _cache_dir(node) -> str:
    """The node's cache folder, or '' if none is set (there is no automatic default)."""
    return (node["cache_dir"].evaluate() or "").strip().replace("\\", "/").rstrip("/")


def _inner(node, name):
    return nuke.toNode(f"{node.fullName()}.{name}")


def _status(node, text: str):
    try:
        node["status"].setValue(text)
    except Exception:
        pass


def _keyed_info(node, cache):
    """(first frame, frame count, colour index) of the key on disk.

    Order of trust: ck_info.json written at run time > cached plate frame numbers > node knobs.
    """
    first = int(node["ck_first"].value())
    colour = int(node["ck_colour"].value()) if node.knob("ck_colour") else 0
    if not cache:                          # no cache folder set -> nothing keyed (never read a relative path)
        return first, 0, colour
    out = os.path.join(cache, "out", "plate_cutout_exr")
    count = len([f for f in os.listdir(out) if f.lower().endswith(".exr")]) if os.path.isdir(out) else 0
    info_path = os.path.join(cache, "ck_info.json")
    try:
        with open(info_path, encoding="utf-8") as f:
            info = json.load(f)
        return int(info["first"]), count, int(info.get("colour", colour))
    except Exception:
        pass
    plate_dir = os.path.join(cache, "plate")
    if os.path.isdir(plate_dir):
        matches = [re.match(r"plate\.(-?\d+)\.exr$", f) for f in os.listdir(plate_dir)]
        nums = [int(m.group(1)) for m in matches if m]
        if nums:
            first = min(nums)
    return first, count, colour


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path.strip().rstrip("/\\"))) if path else ""


def _cache_sharers(node, cache):
    """Other CorridorKey nodes in the script that point at the same cache folder."""
    me = _norm(cache)
    out = []
    for n in nuke.allNodes(recurseGroups=True):
        if n is node or n.knob("ck_first") is None or n.knob("cache_dir") is None:
            continue
        other = _cache_dir(n)
        if other and _norm(other) == me:
            out.append(n)
    return out


def _cache_owner_note(node, cache):
    """Text warning if the key in this cache was made by a different node, else ''."""
    try:
        with open(os.path.join(cache, "ck_info.json"), encoding="utf-8") as f:
            owner = json.load(f).get("node", "")
    except Exception:
        return ""
    if owner and owner != node.fullName():
        return f"note: this cache was last keyed by {owner}"
    return ""


def _clean_dir(path: str):
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)


# --------------------------------------------------------------------------
# node creation
# --------------------------------------------------------------------------

def _knob(group, knob):
    group.addKnob(knob)
    return knob


def _set_srgb_encode(expr_node):
    """Exact inverse of CorridorKey's sRGB decode (IEC 61966-2-1), per channel."""
    for i, c in enumerate(("red", "green", "blue")):
        expr_node[f"channel{i}"].setValue(c)
        v = c[0]
        expr_node[f"expr{i}"].setValue(f"{v} <= 0.0031308 ? 12.92 * {v} : 1.055 * pow({v}, 1/2.4) - 0.055")
    expr_node["channel3"].setValue("none")


def create(connect_selected=True):
    """Create a CorridorKey node.

    From the Tab menu / Nodes toolbar (connect_selected=True) it is made with nuke.createNode, so it behaves
    like any Nuke node: it appears where you last clicked in the Node Graph, or under the selected node and
    connected to it. upgrade() uses connect_selected=False and positions the node itself.
    """
    upstream, downstream = None, []
    if connect_selected:
        try:
            upstream = nuke.selectedNode()
        except ValueError:
            upstream = None
        if upstream is not None:      # nodes fed by the selected node: the new node is inserted before them
            downstream = [(d, i) for d in upstream.dependent(nuke.INPUTS | nuke.HIDDEN_INPUTS, forceEvaluate=False)
                          for i in range(d.inputs()) if d.input(i) is upstream]
        g = nuke.createNode("Group", inpanel=False)
    else:
        for n in nuke.allNodes():
            n.setSelected(False)
        g = nuke.nodes.Group()
    g.setName("CorridorKey1", uncollide=True)
    g["tile_color"].setValue(0x1FA35AFF)
    g["note_font"].setValue("Bold")

    _knob(g, nuke.Tab_Knob("corridorkey", "CorridorKey"))
    _knob(g, nuke.Text_Knob("about", "", "<b>CorridorKey 2.0</b> &nbsp; AI green/blue screen keyer"))
    k = _knob(g, nuke.File_Knob("cache_dir", "cache folder"))
    k.setTooltip("Where the plate frames and the key are stored - one folder per node. "
                 "Required - Run will not start until a folder is set.")
    _knob(g, nuke.PyScript_Knob("open_cache", "open",
                                "import corridorkey_nuke; corridorkey_nuke.open_cache(nuke.thisNode())"))
    run = _knob(g, nuke.PyScript_Knob("run_key", "  Run CorridorKey  ",
                                      "import corridorkey_nuke; corridorkey_nuke.run(nuke.thisNode())"))
    run.setFlag(nuke.STARTLINE)
    _knob(g, nuke.PyScript_Knob("cancel_key", "Cancel",
                                "import corridorkey_nuke; corridorkey_nuke.cancel(nuke.thisNode())"))
    _knob(g, nuke.PyScript_Knob("reload_key", "Reload result",
                                "import corridorkey_nuke; corridorkey_nuke.reload(nuke.thisNode())"))
    k = _knob(g, nuke.PyScript_Knob("make_read", "Create Read + Switch",
                                    "import corridorkey_nuke; corridorkey_nuke.make_read_switch(nuke.thisNode())"))
    k.setTooltip("Build a Read of the keyed EXRs and a Switch (premult FG / straight FG / matte / plate + alpha) "
                 "in the node graph. Done automatically after every run.")
    st = _knob(g, nuke.Text_Knob("status", "status", "not keyed yet - set a cache folder, then press Run CorridorKey"))
    st.setFlag(nuke.STARTLINE)

    _knob(g, nuke.Text_Knob("div_range", "frame range"))
    k = _knob(g, nuke.Int_Knob("range_first", "first"))
    k.setValue(int(nuke.root()["first_frame"].value()))
    k = _knob(g, nuke.Int_Knob("range_last", "last"))
    k.setValue(int(nuke.root()["last_frame"].value()))
    k.clearFlag(nuke.STARTLINE)
    _knob(g, nuke.PyScript_Knob("range_from_input", "from input",
                                "import corridorkey_nuke; corridorkey_nuke.range_from_input(nuke.thisNode())"))
    k = _knob(g, nuke.PyScript_Knob("range_from_viewer", "in/out",
                                    "import corridorkey_nuke; corridorkey_nuke.range_from_viewer(nuke.thisNode())"))
    k.setTooltip("Use the in/out points set in the Viewer timeline.")

    _knob(g, nuke.Text_Knob("div_hint", "hint (optional 2nd input)"))
    _knob(g, nuke.Enumeration_Knob("hint_mode", "hint", HINT_MODES))
    k = _knob(g, nuke.Enumeration_Knob("hint_channel", "channel", HINT_CHANNELS))
    k.clearFlag(nuke.STARTLINE)
    k = _knob(g, nuke.Double_Knob("hint_quality", "hint quality"))
    k.setRange(0, 1)
    k.setValue(0.95)
    k.setTooltip("How much the model trusts the hint. 0.95 clean roto/matte, "
                 "0.75 rough automatic matte, 0.5 or less loose garbage matte.")

    _knob(g, nuke.Text_Knob("div_out", "output"))
    k = _knob(g, nuke.Double_Knob("despill", "despill"))
    k.setRange(0, 1)
    k.setValue(0.5)
    k.setTooltip("Despill strength baked in when the key runs (0 = raw model foreground). Re-run after changing.")
    _knob(g, nuke.Enumeration_Knob("output", "output", OUTPUTS))
    k = _knob(g, nuke.Int_Knob("key_start", "key starts at"))
    k.setValue(1)
    k.setTooltip("The Nuke frame where the first keyed frame is shown. Set automatically to the start of the "
                 "keyed range after every run; change it to slide the key earlier/later. 'reset' puts it back.")
    _knob(g, nuke.PyScript_Knob("key_start_reset", "reset",
                                "import corridorkey_nuke; corridorkey_nuke.reset_start(nuke.thisNode())"))
    k = _knob(g, nuke.Double_Knob("clip_black", "clip black"))
    k.setRange(0, 0.5)
    k.setTooltip("Live: alpha below this becomes 0 (cleans background noise). No re-run needed.")
    k = _knob(g, nuke.Double_Knob("clip_white", "clip white"))
    k.setRange(0.5, 1)
    k.setValue(1.0)
    k.setTooltip("Live: alpha above this becomes 1 (fills core holes). No re-run needed.")

    _knob(g, nuke.Text_Knob("div_adv", "advanced"))
    k = _knob(g, nuke.Enumeration_Knob("model_colour", "model colour", MODEL_COLOURS))
    k.setTooltip("What the model sees. 'viewer look' = your plate through the OCIO color_picking role "
                 "(the display look the model was trained on). Converted back to the working space afterwards.")
    k = _knob(g, nuke.Enumeration_Knob("memory", "GPU memory", MEMORY))
    k.setTooltip("auto picks the temporal window from free VRAM. Use low VRAM if you get CUDA out of memory.")
    k = _knob(g, nuke.Int_Knob("hann_chunk", "window"))
    k.setValue(0)
    k.clearFlag(nuke.STARTLINE)
    k.setTooltip("Temporal window in frames (0 = automatic). Smaller uses less VRAM.")

    # hidden state
    for name, val in (("ck_first", 1), ("ck_count", 0), ("ck_ready", 0), ("ck_rendering", 0), ("ck_colour", 0)):
        k = _knob(g, nuke.Int_Knob(name, name))
        k.setValue(val)
        k.setFlag(nuke.INVISIBLE)

    _knob(g, nuke.Tab_Knob("howto", "How to use"))
    _knob(g, nuke.Text_Knob("help_text", "", HELP_HTML))
    g["knobChanged"].setValue("import corridorkey_nuke; corridorkey_nuke.knob_changed(nuke.thisNode(), nuke.thisKnob())")

    with g:
        plate = nuke.nodes.Input(name="Plate", number=0, xpos=0, ypos=-400)
        hint = nuke.nodes.Input(name="Hint", number=1, xpos=400, ypos=-400)

        # --- plate cache: working space -> display-referred (what the model expects), written raw
        to_model = nuke.nodes.OCIOColorSpace(name="To_model", inputs=[plate], xpos=200, ypos=-340)
        _set_colourspace(to_model, "in_colorspace", WORKING_SPACE_CANDIDATES)
        _set_colourspace(to_model, "out_colorspace", [MODEL_ROLES[0]])
        crop_p = nuke.nodes.Crop(name="Crop_plate", inputs=[to_model], xpos=200, ypos=-300, reformat=False, crop=True)
        crop_p["box"].setExpression("0", 0)
        crop_p["box"].setExpression("0", 1)
        crop_p["box"].setExpression("input.width", 2)
        crop_p["box"].setExpression("input.height", 3)
        wp = nuke.nodes.Write(name="Write_plate", inputs=[crop_p], xpos=200, ypos=-240)
        wp["file_type"].setValue("exr")
        wp["channels"].setValue("rgb")
        wp["raw"].setValue(True)
        wp["datatype"].setValue("16 bit half")
        wp["compression"].setValue("Zip (1 scanline)")
        wp["create_directories"].setValue(True)
        wp["file"].setValue("[value parent.cache_dir]/plate/plate.%06d.exr")
        wp["disable"].setExpression("!parent.ck_rendering")

        # --- hint cache: chosen channel copied into rgb, on the plate's format
        # AddChannels guarantee rgba exists on both sides, so Copy_hint never errors
        # (unconnected node, or a hint without alpha / red)
        plate_ch = nuke.nodes.AddChannels(name="AddChannels_plate", inputs=[plate], channels="rgba",
                                          xpos=300, ypos=-340)
        hint_ch = nuke.nodes.AddChannels(name="AddChannels_hint", inputs=[hint], channels="rgba",
                                         xpos=400, ypos=-340)
        cp = nuke.nodes.Copy(name="Copy_hint", inputs=[plate_ch, hint_ch], xpos=400, ypos=-300)
        cp["from0"].setValue("rgba.red")
        cp["to0"].setValue("rgba.red")
        cp["from1"].setValue("rgba.alpha")
        cp["to1"].setValue("rgba.alpha")
        ex = nuke.nodes.Expression(name="Expr_hint", inputs=[cp], xpos=400, ypos=-260)
        ex["channel0"].setValue("rgb")
        ex["expr0"].setValue("clamp(parent.hint_channel == 0 ? a : r)")
        crop_h = nuke.nodes.Crop(name="Crop_hint", inputs=[ex], xpos=400, ypos=-220, reformat=False, crop=True)
        crop_h["box"].setExpression("input.width", 2)
        crop_h["box"].setExpression("input.height", 3)
        wh = nuke.nodes.Write(name="Write_hint", inputs=[crop_h], xpos=400, ypos=-180)
        wh["file_type"].setValue("exr")
        wh["channels"].setValue("rgb")
        wh["raw"].setValue(True)
        wh["datatype"].setValue("16 bit half")
        wh["compression"].setValue("Zip (1 scanline)")
        wh["create_directories"].setValue(True)
        wh["file"].setValue("[value parent.cache_dir]/hint/hint.%06d.exr")
        wh["disable"].setExpression("!parent.ck_rendering")

        # --- result: straight linear RGB + alpha written by CorridorKey
        rd = nuke.nodes.Read(name="Read_key", xpos=-200, ypos=-300)
        rd["file"].setValue("")   # filled in by _point_read() once a key exists (a missing file errors in the GUI)
        rd["raw"].setValue(True)
        rd["first"].setExpression("0")
        rd["last"].setExpression("max(0, parent.ck_count - 1)")
        rd["origfirst"].setExpression("0")
        rd["origlast"].setExpression("max(0, parent.ck_count - 1)")
        rd["frame_mode"].setValue("start at")
        rd["frame"].setValue("1")  # set to the keyed range's first frame by _point_read()
        rd["before"].setValue("black")
        rd["after"].setValue("black")
        rd["on_error"].setValue("black")
        rd["disable"].setExpression("!parent.ck_ready")   # no key yet -> no missing-file error

        # model foreground (linear from its sRGB decode) -> sRGB -> back to working space
        # rgba always present downstream, even while Read_key is disabled (not keyed yet)
        rd_ch = nuke.nodes.AddChannels(name="AddChannels_key", inputs=[rd], channels="rgba", xpos=-200, ypos=-285)
        enc = nuke.nodes.Expression(name="Encode_sRGB", inputs=[rd_ch], xpos=-200, ypos=-270)
        _set_srgb_encode(enc)
        from_model = nuke.nodes.OCIOColorSpace(name="From_model", inputs=[enc], xpos=-200, ypos=-240)
        _set_colourspace(from_model, "in_colorspace", [MODEL_ROLES[0]])
        _set_colourspace(from_model, "out_colorspace", WORKING_SPACE_CANDIDATES)
        clip = nuke.nodes.Grade(name="Clip_matte", inputs=[from_model], xpos=-200, ypos=-210)
        clip["channels"].setValue("alpha")
        clip["blackpoint"].setExpression("parent.clip_black")
        clip["whitepoint"].setExpression("parent.clip_white")
        clip["black_clamp"].setValue(True)
        clip["white_clamp"].setValue(True)
        key = clip

        premult = nuke.nodes.Premult(name="Premult_fg", inputs=[key], xpos=-300, ypos=-150)
        matte = nuke.nodes.Expression(name="Matte_only", inputs=[key], xpos=-200, ypos=-150)
        matte["channel0"].setValue("rgb")
        matte["expr0"].setValue("a")
        plate_a = nuke.nodes.Copy(name="Plate_alpha", inputs=[plate, key], xpos=-100, ypos=-150)
        plate_a["from0"].setValue("rgba.alpha")
        plate_a["to0"].setValue("rgba.alpha")

        sw_out = nuke.nodes.Switch(name="Switch_output", inputs=[premult, key, matte, plate_a], xpos=-200, ypos=-100)
        sw_out["which"].setExpression("parent.output")

        sw_ready = nuke.nodes.Switch(name="Switch_ready", inputs=[plate, sw_out], xpos=0, ypos=0)
        sw_ready["which"].setExpression("parent.ck_ready")

        nuke.nodes.Output(name="Output1", inputs=[sw_ready], xpos=0, ypos=100)

    # createNode tried to connect the selected node before the Group had inputs - connect it now
    if upstream is not None and g.input(0) is None and upstream is not g:
        g.setInput(0, upstream)
    for d, i in downstream:
        if d is not g and d.input(i) is upstream:
            d.setInput(i, g)
    g.setSelected(True)
    if connect_selected and nuke.GUI:
        g.showControlPanel()
    return g


# --------------------------------------------------------------------------
# buttons
# --------------------------------------------------------------------------

def range_from_input(node):
    src = node.input(0)
    if src is None:
        nuke.message("Connect a plate to the CorridorKey node first.")
        return
    node["range_first"].setValue(int(src.firstFrame()))
    node["range_last"].setValue(int(src.lastFrame()))


def range_from_viewer(node):
    viewer = None
    try:
        av = nuke.activeViewer()
        viewer = av.node() if av else None
    except Exception:
        viewer = None
    if viewer is None:
        viewers = nuke.allNodes("Viewer")
        viewer = viewers[0] if viewers else None
    rng = viewer["frame_range"].value().strip() if viewer is not None and viewer.knob("frame_range") else ""
    m = re.match(r"^\s*(-?\d+)\s*-\s*(-?\d+)\s*$", rng)
    if not m:
        nuke.message("No in/out range found on the Viewer.\nSet in and out points in the Viewer timeline first.")
        return
    node["range_first"].setValue(int(m.group(1)))
    node["range_last"].setValue(int(m.group(2)))


def open_cache(node):
    path = _cache_dir(node)
    if not path:
        nuke.message(CACHE_REQUIRED_MSG)
        return
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        nuke.message(f"Can't create the cache folder:\n{path}\n\n{exc}")
        return
    if sys.platform == "win32":
        os.startfile(os.path.normpath(path))
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])


def reload(node):
    if not _cache_dir(node):
        nuke.message(CACHE_REQUIRED_MSG)
        return
    first, count, colour = _keyed_info(node, _cache_dir(node))
    if count == 0:
        nuke.message("No CorridorKey result in the cache folder yet. Press Run CorridorKey.")
        return
    if node.knob("ck_colour"):
        node["ck_colour"].setValue(colour)
    keep = int(node["ck_ready"].value()) == 1 and int(node["ck_first"].value()) == first
    _point_read(node, first, count, keep_start=keep)
    note = _cache_owner_note(node, _cache_dir(node))
    _status(node, f"keyed {count} frames ({first}-{first + count - 1})" + (f" - {note}" if note else ""))
    if note:
        nuke.message(f"{node.name()}: {note}.\nThe key shown may not be this node's own key.")


def _point_read(node, first, count, keep_start=False):
    """Show `count` keyed frames; the key starts at `first` unless the user's manual start is kept."""
    node["ck_first"].setValue(first)
    node["ck_count"].setValue(count)
    node["ck_ready"].setValue(1)
    if node.knob("key_start") is not None and not keep_start:
        node["key_start"].setValue(int(first))
    fm = _inner(node, "From_model")
    if fm is not None:
        _set_colourspace(fm, "in_colorspace", [MODEL_ROLES[int(node["ck_colour"].value())]])
    _apply_start(node)
    rd = _inner(node, "Read_key")
    if rd is not None:
        rd["file"].setValue(KEY_FILE)
        rd["reload"].execute()


def _detach_reads(node):
    """Empty the inner and outside Reads (before the key files are deleted for a new run)."""
    node["ck_ready"].setValue(0)
    rd = _inner(node, "Read_key")
    if rd is not None:
        rd["file"].setValue("")
    with _parent_group(node):
        out = nuke.toNode(f"{node.name()}_Read")
        if out is not None and out.Class() == "Read":
            out["file"].setValue("")


def _start(node):
    k = node.knob("key_start")
    return int(k.value()) if k is not None else int(node["ck_first"].value())


def _apply_start(node):
    """Push the start frame to the inner Read and the outside Read (their 'start at' needs a literal)."""
    start = str(_start(node))
    rd = _inner(node, "Read_key")
    if rd is not None:
        rd["frame"].setValue(start)
    with _parent_group(node):
        out = nuke.toNode(f"{node.name()}_Read")
        if out is not None and out.Class() == "Read":
            out["frame"].setValue(start)
            count = int(node["ck_count"].value())
            out["label"].setValue(f"CorridorKey result\nframes {start}-{int(start) + count - 1}")


def reset_start(node):
    first, count, _colour = _keyed_info(node, _cache_dir(node))
    node["key_start"].setValue(first)
    _apply_start(node)


def knob_changed(node, knob):
    if knob is not None and knob.name() == "key_start":
        _apply_start(node)


def _parent_group(node):
    parts = node.fullName().split(".")
    return nuke.toNode(".".join(parts[:-1])) if len(parts) > 1 else nuke.root()


def _get_or_make(cls, name, **kw):
    n = nuke.toNode(name)
    if n is None or n.Class() != cls:
        n = getattr(nuke.nodes, cls)(name=name, **kw)
    return n


def _size(n):
    """On-screen node size (falls back to typical sizes when Nuke runs without a GUI)."""
    w, h = n.screenWidth(), n.screenHeight()
    if w <= 0 or h <= 0:
        cls = n.Class()
        w, h = {"Read": (80, 100), "Dot": (12, 12), "BackdropNode": (0, 0)}.get(cls, (100, 34))
    return w, h


def _put(n, cx, top):
    """Place a node with its centre at cx and its top at `top`; return its bottom."""
    w, h = _size(n)
    n.setXYpos(int(cx - w / 2), int(top))
    return top + h


def _layout_outputs(node, column, branches, switch, backdrop):
    """Plate above the CorridorKey node on the left; result column, output branches and Switch on the right;
    everything inside one backdrop. The CorridorKey node stays where it is."""
    nw, nh = _size(node)
    left_cx = node.xpos() + nw / 2
    plate = node.input(0)
    if plate is not None:
        pw, ph = _size(plate)
        plate.setXYpos(int(left_cx - pw / 2), int(node.ypos() - 70 - ph))
    cx = left_cx + 380                    # result column centre, to the right of the CorridorKey node
    top = plate.ypos() if plate is not None else node.ypos()
    gap = 36
    for n in column:                      # Read, encode, toWorking, clip, Dot
        top = _put(n, cx, top) + gap
    row_top = top + 30
    spacing = 190
    # Switch input 1 (straight) comes straight down from the Dot; branches sit left and right of it
    offsets = (-spacing, spacing, 2 * spacing)      # premult, matte, plate + alpha
    row_bottom = row_top
    for n, off in zip(branches, offsets):
        row_bottom = max(row_bottom, _put(n, cx + off, row_top))
    sw_bottom = _put(switch, cx, row_bottom + 90)

    nodes = column + branches + [switch, node] + ([plate] if plate is not None else [])
    x0 = min(n.xpos() for n in nodes) - 80
    x1 = max(n.xpos() + _size(n)[0] for n in nodes) + 80
    y0 = min(n.ypos() for n in nodes) - 90        # room for the title
    y1 = max(n.ypos() + _size(n)[1] for n in nodes) + 60
    backdrop.setXYpos(int(x0), int(y0))
    backdrop["bdwidth"].setValue(int(x1 - x0))
    backdrop["bdheight"].setValue(int(y1 - y0))
    backdrop["label"].setValue(f"<center>{node.name()}")
    backdrop["note_font_size"].setValue(30)
    backdrop["tile_color"].setValue(0x26402FFF)
    if backdrop.knob("z_order"):
        backdrop["z_order"].setValue(-1)


def make_read_switch(node, quiet=False):
    """Create (or update) a Read of the keyed EXRs + output Switch next to the CorridorKey node."""
    cache = _cache_dir(node)
    first, count, colour = _keyed_info(node, cache)
    if count == 0:
        if not quiet:
            nuke.message(CACHE_REQUIRED_MSG if not cache else
                         "No CorridorKey result in the cache folder yet. Press Run CorridorKey first.")
        return None
    note = _cache_owner_note(node, cache)
    if note and not quiet:
        nuke.message(f"{node.name()}: {note}.\nThe Read will show that key, not one made by {node.name()}.")
    if int(node["ck_first"].value()) != first or int(node["ck_count"].value()) != count:
        if node.knob("ck_colour"):
            node["ck_colour"].setValue(colour)
        _point_read(node, first, count)       # keep the node itself in step too
    base = node.name()
    x, y = node.xpos(), node.ypos()

    with _parent_group(node):
        rd = _get_or_make("Read", f"{base}_Read")
        rd["file"].setValue(f"{cache}/out/plate_cutout_exr/cutout_######.exr".replace("\\", "/"))
        rd["raw"].setValue(True)
        for k in ("first", "origfirst"):
            rd[k].setValue(0)
        for k in ("last", "origlast"):
            rd[k].setValue(count - 1)
        rd["frame_mode"].setValue("start at")
        start = _start(node)
        rd["frame"].setValue(str(start))
        rd["before"].setValue("black")
        rd["after"].setValue("black")
        rd["on_error"].setValue("black")
        rd["label"].setValue(f"CorridorKey result\nframes {start}-{start + count - 1}")
        rd["tile_color"].setValue(0x1FA35AFF)
        rd["reload"].execute()

        # same colour return path as inside the node: model linear -> sRGB -> working space
        enc = _get_or_make("Expression", f"{base}_encodeSRGB")
        _set_srgb_encode(enc)
        enc.setInput(0, rd)
        fm = _get_or_make("OCIOColorSpace", f"{base}_toWorking")
        _set_colourspace(fm, "in_colorspace", [MODEL_ROLES[int(node["ck_colour"].value())]])
        _set_colourspace(fm, "out_colorspace", WORKING_SPACE_CANDIDATES)
        fm.setInput(0, enc)
        cl = _get_or_make("Grade", f"{base}_clipMatte")
        cl["channels"].setValue("alpha")
        cl["blackpoint"].setExpression(f"{base}.clip_black")
        cl["whitepoint"].setExpression(f"{base}.clip_white")
        cl["black_clamp"].setValue(True)
        cl["white_clamp"].setValue(True)
        cl["label"].setValue(f"clip linked to {base}")
        cl.setInput(0, fm)
        stale = nuke.toNode(f"{base}_edgeDetail")      # removed feature: delete it if an earlier build made one
        if stale is not None:
            nuke.delete(stale)
        dot = _get_or_make("Dot", f"{base}_dot")
        dot.setInput(0, cl)
        column = [rd, enc, fm, cl, dot]
        key = dot

        pm = _get_or_make("Premult", f"{base}_premult")
        pm.setInput(0, key)

        mt = _get_or_make("Expression", f"{base}_matte")
        mt["channel0"].setValue("rgb")
        mt["expr0"].setValue("a")
        mt.setInput(0, key)

        pa = _get_or_make("Copy", f"{base}_plateAlpha")
        pa["from0"].setValue("rgba.alpha")
        pa["to0"].setValue("rgba.alpha")
        pa.setInput(0, node.input(0))   # B = original plate
        pa.setInput(1, key)             # A = key

        sw = _get_or_make("Switch", f"{base}_Switch")
        for i, src in enumerate((pm, key, mt, pa)):
            sw.setInput(i, src)
        sw["which"].setValue(int(node["output"].getValue()))
        sw["label"].setValue("[lindex {{premult FG} {straight FG} {matte} {plate + alpha}} [value which]]")
        sw["tile_color"].setValue(0x1FA35AFF)

        bd = _get_or_make("BackdropNode", f"{base}_outputBackdrop")
        _layout_outputs(node, column, [pm, mt, pa], sw, bd)

    for n in nuke.selectedNodes():
        n.setSelected(False)
    sw.setSelected(True)
    return sw


_UPGRADE_KNOBS = ("cache_dir", "range_first", "range_last", "hint_mode", "hint_channel", "hint_quality",
                  "despill", "output", "clip_black", "clip_white", "model_colour", "memory", "hann_chunk",
                  "label", "note_font_size")


def upgrade(old):
    """Replace an older CorridorKey node with the current version, keeping settings and connections."""
    name, x, y = old.name(), old.xpos(), old.ypos()
    values = {k: old[k].value() for k in _UPGRADE_KNOBS if old.knob(k) is not None}
    current = _inner(old, "To_model") is not None     # already has the ACES-aware colour pipeline
    state = {k: old[k].value() for k in ("ck_first", "ck_count", "ck_colour") if old.knob(k) is not None}
    manual_start = int(old["key_start"].value()) if old.knob("key_start") is not None else None
    inputs = [old.input(i) for i in range(old.inputs())]
    users = [(d, i) for d in old.dependent(nuke.INPUTS | nuke.HIDDEN_INPUTS, forceEvaluate=False)
             for i in range(d.inputs()) if d.input(i) is old]
    with _parent_group(old):
        new = create(connect_selected=False)
        for k, v in values.items():
            try:
                new[k].setValue(int(v) if isinstance(new[k], nuke.Int_Knob) else v)
            except Exception as exc:
                nuke.tprint(f"[CorridorKey] upgrade: could not copy {k}={v!r}: {exc}")
        for i, src in enumerate(inputs):
            if src is not None:
                new.setInput(i, src)
        for d, i in users:
            d.setInput(i, new)
        nuke.delete(old)
        new.setName(name)
        new.setXYpos(x, y)
        stale = nuke.toNode(f"{name}_edgeDetail")      # removed feature: splice it out of the outside chain
        if stale is not None:
            src = stale.input(0)
            for d in stale.dependent(nuke.INPUTS | nuke.HIDDEN_INPUTS, forceEvaluate=False):
                for i in range(d.inputs()):
                    if d.input(i) is stale:
                        d.setInput(i, src)
            nuke.delete(stale)
        for k, v in state.items():
            new[k].setValue(int(v))
    if current:
        first, count, colour = _keyed_info(new, _cache_dir(new))
        if count:
            new["ck_colour"].setValue(colour)
            _point_read(new, first, count)
            if manual_start is not None:
                new["key_start"].setValue(manual_start)
                _apply_start(new)
            _status(new, f"updated - keyed {count} frames ({first}-{first + count - 1})")
        else:
            _status(new, "updated - not keyed yet")
    else:
        _status(new, "updated - press Run CorridorKey again (new colour pipeline)")
    return new


def cancel(node):
    job = _jobs.get(node.fullName())
    if job is None or not job.running:
        nuke.message("No CorridorKey job is running for this node.")
        return
    job.cancel()


def run(node):
    job = _jobs.get(node.fullName())
    if job is not None and job.running:
        nuke.message("CorridorKey is already running for this node.")
        return
    prepared = prepare(node)
    if prepared is None:
        return
    cmd, cache, first, n_frames = prepared
    job = _Job(node, cmd, cache, first, n_frames)
    _jobs[node.fullName()] = job
    job.start()


def prepare(node):
    """Render the plate/hint cache and return (cmd, cache, first, n_frames), or None."""
    py = venv_python()
    if not os.path.isfile(py) or not os.path.isfile(os.path.join(backend_dir(), "infer.py")):
        nuke.message("CorridorKey 2.0 backend not found.\n\n"
                     f"Looked in: {backend_dir()}\n\n"
                     "Install it with install/install_backend.py from github.com/gireshahid33/CorridorKey2.0-Nuke, or point the "
                     "plugin at an existing install via Nodes > CorridorKey > Set backend folder...")
        return None
    if node.input(0) is None:
        nuke.message("Connect a plate (green/blue screen) to the Plate input.")
        return None

    first = int(node["range_first"].value())
    last = int(node["range_last"].value())
    if last < first:
        nuke.message("Frame range: last is before first.")
        return None
    hint_mode = int(node["hint_mode"].getValue())
    if hint_mode and node.input(1) is None:
        nuke.message("Hint mode is on but nothing is connected to the Hint input.")
        return None

    to_model = _inner(node, "To_model")
    if to_model is None:
        nuke.message("This CorridorKey node is from an older version.\n"
                     "Select it and use Nodes > CorridorKey > Update selected CorridorKey nodes, then run again.")
        return None

    cache = _cache_dir(node)
    if not cache:
        _status(node, "not run - set a cache folder first")
        nuke.message(CACHE_REQUIRED_MSG)
        return None
    try:
        os.makedirs(cache, exist_ok=True)
    except OSError as exc:
        _status(node, "not run - cache folder can't be created")
        nuke.message(f"Can't create the cache folder:\n{cache}\n\n{exc}\n\nPick a folder on a drive that exists.")
        return None
    sharers = _cache_sharers(node, cache)
    if sharers:
        names = ", ".join(n.name() for n in sharers)
        own = f"{cache.rstrip('/')}/{_safe_name(node.name())}"
        if not nuke.ask(f"{names} uses the same cache folder:\n{cache}\n\n"
                        f"Running {node.name()} here would overwrite its key.\n\n"
                        f"Use a separate folder for {node.name()} instead?\n{own}"):
            _status(node, "not run - cache folder is shared with " + names)
            return None
        node["cache_dir"].setValue(own)
        cache = own
    _detach_reads(node)                      # before the old key files are deleted
    for sub in ("plate", "hint", "out", "tmp"):
        _clean_dir(os.path.join(cache, sub))
    colour = int(node["model_colour"].getValue())
    node["ck_colour"].setValue(colour)
    _set_colourspace(to_model, "out_colorspace", [MODEL_ROLES[colour]])

    # 1) render plate (and hint) to EXR inside Nuke - always at full resolution
    _status(node, "rendering plate to cache...")
    node["ck_rendering"].setValue(1)
    proxy_was_on = bool(nuke.root()["proxy"].value())
    if proxy_was_on:
        nuke.root()["proxy"].setValue(False)
    try:
        nuke.execute(_inner(node, "Write_plate"), first, last, 1)
        if hint_mode == 1:
            nuke.execute(_inner(node, "Write_hint"), first, first, 1)
        elif hint_mode == 2:
            nuke.execute(_inner(node, "Write_hint"), first, last, 1)
    except RuntimeError as exc:
        _status(node, f"render failed / cancelled: {exc}")
        return None
    finally:
        node["ck_rendering"].setValue(0)
        if proxy_was_on:
            nuke.root()["proxy"].setValue(True)

    plate_dir = os.path.join(cache, "plate")
    hint_dir = os.path.join(cache, "hint")
    out_dir = os.path.join(cache, "out")
    n_frames = last - first + 1
    with open(os.path.join(cache, "ck_info.json"), "w", encoding="utf-8") as f:
        json.dump({"first": first, "last": last, "colour": colour, "colour_role": MODEL_ROLES[colour],
                   "node": node.fullName(), "script": nuke.root().name()}, f, indent=1)

    cmd = [
        py, "-u", os.path.join(backend_dir(), "infer.py"),
        "--input", plate_dir,
        "--output_dir", out_dir,
        "--frame_dir_fps", str(nuke.root()["fps"].value()),
        "--output_format", "exr",
        "--outputs", "cutout",
        "--despill_strength", f"{node['despill'].value():.3f}",
        "--temp_dir", os.path.join(cache, "tmp"),
        "--ffmpeg_bin", _find_ffmpeg(),
    ]
    if hint_mode == 1:
        cmd += ["--hint_first_frame", os.path.join(hint_dir, f"hint.{first:06d}.exr"),
                "--hint_quality", f"{node['hint_quality'].value():.3f}"]
    elif hint_mode == 2:
        cmd += ["--hint_video", hint_dir, "--hint_quality", f"{node['hint_quality'].value():.3f}"]
    chunk = int(node["hann_chunk"].value())
    if int(node["memory"].getValue()) == 1:
        cmd += ["--low_vram"]
        if chunk <= 0:
            chunk = 4
    if chunk > 0:
        cmd += ["--hann_chunk", str(chunk), "--hann_stride", str(max(1, chunk // 2))]
    return cmd, cache, first, n_frames


def subprocess_env():
    env = dict(os.environ)
    for var in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE"):
        env.pop(var, None)
    # Nuke sets LD_LIBRARY_PATH to its own bundled libraries (libstdc++, openssl, ...).
    # If the backend's conda Python and torch see them first they can crash, so drop them.
    if sys.platform != "win32":
        env.pop("LD_LIBRARY_PATH", None)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONNOUSERSITE"] = "1"
    env["HF_HOME"] = os.path.join(backend_dir(), "_cache", "hf")
    env["PATH"] = os.pathsep.join(_python_dirs()) + os.pathsep + env.get("PATH", "")
    return env


# --------------------------------------------------------------------------
# background job
# --------------------------------------------------------------------------

class _Job:
    def __init__(self, node, cmd, cache, first, n_frames):
        self.node_name = node.fullName()
        self.cmd = cmd
        self.cache = cache
        self.first = first
        self.n_frames = n_frames
        self.proc = None
        self.running = False
        self.cancelled = False
        self.log_path = os.path.join(cache, "corridorkey_log.txt")

    def _node(self):
        return nuke.toNode(self.node_name)

    def _ui(self, fn, *args):
        nuke.executeInMainThread(fn, args)

    def _set_status(self, text):
        def apply(t):
            n = self._node()
            if n is not None:
                _status(n, t)
        self._ui(apply, text)

    def start(self):
        self.proc = subprocess.Popen(
            self.cmd, cwd=backend_dir(), env=subprocess_env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.running = True
        self._set_status("CorridorKey: loading model...")
        threading.Thread(target=self._watch, daemon=True).start()

    def cancel(self):
        self.cancelled = True
        if self.proc and self.proc.poll() is None:
            self.proc.kill()

    def _watch(self):
        task = nuke.ProgressTask(f"CorridorKey  {self.node_name}")
        out_dir = os.path.join(self.cache, "out", "plate_cutout_exr")
        tail = []
        stage1 = 0.0
        lines_q = []

        def reader():
            for line in self.proc.stdout:
                lines_q.append(line)
        rt = threading.Thread(target=reader, daemon=True)
        rt.start()

        with open(self.log_path, "w", encoding="utf-8") as log:
            log.write(" ".join(f'"{c}"' if " " in c else c for c in self.cmd) + "\n\n")
            while True:
                if task.isCancelled() and not self.cancelled:
                    self.cancel()
                while lines_q:
                    line = lines_q.pop(0)
                    log.write(line)
                    log.flush()
                    s = line.strip()
                    if s:
                        tail = (tail + [s])[-25:]
                    m = re.search(r"window (\d+)/(\d+)", s)
                    if m:
                        stage1 = int(m.group(1)) / max(1, int(m.group(2)))
                        task.setMessage(f"temporal pass  {m.group(1)}/{m.group(2)}")
                        self._set_status(f"CorridorKey: temporal pass {m.group(1)}/{m.group(2)}")
                    elif "prepass" in s:
                        task.setMessage(s)
                        self._set_status("CorridorKey: " + s)
                done = len(os.listdir(out_dir)) if os.path.isdir(out_dir) else 0
                pct = 5 + 45 * stage1 + 50 * done / max(1, self.n_frames)
                if done:
                    task.setMessage(f"writing frames  {done}/{self.n_frames}")
                task.setProgress(int(min(99, pct)))
                if self.proc.poll() is not None and not rt.is_alive() and not lines_q:
                    break
                time.sleep(0.25)

        code = self.proc.returncode
        self.running = False
        del task
        done = len([f for f in os.listdir(out_dir) if f.endswith(".exr")]) if os.path.isdir(out_dir) else 0

        if self.cancelled:
            self._set_status("cancelled")
            return
        if code != 0 or done == 0:
            msg = "\n".join(tail[-12:])
            self._set_status(f"FAILED (exit {code}) - see corridorkey_log.txt in the cache folder")
            self._ui(nuke.message, f"CorridorKey failed (exit {code}).\n\n{msg}\n\nLog: {self.log_path}")
            return

        def finish(count, first):
            n = self._node()
            if n is None:
                return
            _point_read(n, first, count)
            try:
                make_read_switch(n, quiet=True)
            except Exception as exc:  # never lose the key over a layout problem
                nuke.tprint(f"[CorridorKey] could not create Read + Switch: {exc}")
            _status(n, f"keyed {count} frames ({first}-{first + count - 1})")
        self._ui(finish, done, self.first)
