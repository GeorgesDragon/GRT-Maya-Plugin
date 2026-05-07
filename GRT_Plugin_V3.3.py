# GRT Rigging Tools Plugin
# Python API 2.0, MPxCommand-based commands + UI wrapper

import maya.cmds as cmds
import maya.api.OpenMaya as om

PLUGIN_NAME = "GRT_RiggingTools"
VENDOR = "GRT"
VERSION = "1.2.1"


def maya_useNewAPI():
    pass


# =========================
# Utility: MObject / Selection
# =========================

def _get_dag_path(node):
    sel = om.MSelectionList()
    sel.add(node)
    return sel.getDagPath(0)


def _is_transform(node):
    return cmds.objExists(node) and cmds.nodeType(node) == "transform"


def _get_children_transforms(root, include_self=True):
    result = []
    if include_self and _is_transform(root):
        result.append(root)
    for child in cmds.listRelatives(root, ad=True, fullPath=True) or []:
        if _is_transform(child):
            result.append(child)
    return result


def _dag_depth(node):
    return len(node.split("|"))


# =========================
# Utility: Namespace Handling
# =========================

_FAKE_NS_CACHE = {}


def _has_fake_namespace(node):
    if ":" not in node:
        return False
    ns = node.split(":", 1)[0]
    return not cmds.namespace(exists=ns)


def _sanitize_fake_namespace(node):
    if not _has_fake_namespace(node):
        return node

    if node in _FAKE_NS_CACHE:
        return _FAKE_NS_CACHE[node]

    msg = "Fake namespace detected:\n\n{}\n\nSanitize name (replace ':' with '_')?".format(node)
    res = cmds.confirmDialog(
        title="GRT Namespace Warning",
        message=msg,
        button=["Sanitize", "Cancel"],
        defaultButton="Sanitize",
        cancelButton="Cancel",
        dismissString="Cancel"
    )
    if res == "Cancel":
        _FAKE_NS_CACHE[node] = None
        return None

    new_name = cmds.rename(node, node.replace(":", "_"))
    _FAKE_NS_CACHE[node] = new_name
    return new_name


# =========================
# Utility: Rig-Safe Checks
# =========================

def _is_attr_locked(node, attr):
    plug = "{}.{}".format(node, attr)
    if not cmds.objExists(plug):
        return True
    return cmds.getAttr(plug, lock=True)


def _is_attr_keyable(node, attr):
    plug = "{}.{}".format(node, attr)
    if not cmds.objExists(plug):
        return False
    return cmds.getAttr(plug, keyable=True)


def _has_constraint(node):
    cons = cmds.listConnections(node, s=True, d=False, type="constraint") or []
    if cons:
        return True

    drivers = cmds.listConnections(node, s=True, d=False) or []
    for d in drivers:
        t = cmds.nodeType(d)
        if t in ("pairBlend", "blendWeighted", "animCurveTL", "animCurveTA", "animCurveTU"):
            return True
    return False


def _is_node_rig_safe(node):
    """
    Node-level rig-safe decision.
    One-line-per-node feedback.
    """
    if not _is_transform(node):
        return False

    if _has_constraint(node):
        om.MGlobal.displayWarning(f"GRT Rig-Safe: Skipped node '{node}' (constrained)")
        return False

    for attr in ("translateX", "translateY", "translateZ",
                 "rotateX", "rotateY", "rotateZ",
                 "scaleX", "scaleY", "scaleZ"):
        if not cmds.objExists(f"{node}.{attr}"):
            om.MGlobal.displayWarning(f"GRT Rig-Safe: Skipped node '{node}' (missing TRS channels)")
            return False
        if _is_attr_locked(node, attr):
            om.MGlobal.displayWarning(f"GRT Rig-Safe: Skipped node '{node}' (locked channels)")
            return False
        if not _is_attr_keyable(node, attr):
            om.MGlobal.displayWarning(f"GRT Rig-Safe: Skipped node '{node}' (non-keyable channels)")
            return False

    return True


# =========================
# Utility: Matrix Helpers
# =========================

def _get_world_matrix(node):
    dag = _get_dag_path(node)
    return dag.inclusiveMatrix()


def _get_parent_inverse_matrix(node):
    dag = _get_dag_path(node)
    if dag.length() > 1:
        parent = om.MDagPath(dag)
        parent.pop()
        return parent.inclusiveMatrixInverse()
    return om.MMatrix.kIdentity


def _get_local_matrix(node):
    world = _get_world_matrix(node)
    parent_inv = _get_parent_inverse_matrix(node)
    return world * parent_inv


def _unlock_trs(node):
    locked = []
    for attr in ["translateX", "translateY", "translateZ",
                 "rotateX", "rotateY", "rotateZ",
                 "scaleX", "scaleY", "scaleZ"]:
        plug = f"{node}.{attr}"
        if not cmds.objExists(plug):
            continue
        if cmds.getAttr(plug, lock=True):
            locked.append(plug)
            cmds.setAttr(plug, lock=False)
    return locked


def _relock(locked):
    for plug in locked:
        if cmds.objExists(plug):
            cmds.setAttr(plug, lock=True)


def _set_trs_from_matrix(node, local_matrix):
    mtm = om.MTransformationMatrix(local_matrix)

    locked = _unlock_trs(node)
    try:
        sx, sy, sz = mtm.scale(om.MSpace.kTransform)
        try:
            cmds.setAttr("{}.scale".format(node), sx, sy, sz, type="double3")
        except RuntimeError:
            om.MGlobal.displayWarning("GRT: Failed to set scale on {}".format(node))
            return

        if not cmds.objExists("{}.rotateOrder".format(node)):
            om.MGlobal.displayWarning("GRT: Node {} has no rotateOrder, skipping.".format(node))
            return

        ro = cmds.getAttr("{}.rotateOrder".format(node))
        euler = mtm.rotation()
        euler = euler.reorder(ro)
        rx = om.MAngle(euler.x).asDegrees()
        ry = om.MAngle(euler.y).asDegrees()
        rz = om.MAngle(euler.z).asDegrees()

        try:
            cmds.setAttr("{}.rotate".format(node), rx, ry, rz, type="double3")
        except RuntimeError:
            om.MGlobal.displayWarning("GRT: Failed to set rotate on {}".format(node))
            return

        trans = mtm.translation(om.MSpace.kTransform)
        try:
            cmds.setAttr("{}.translate".format(node), trans.x, trans.y, trans.z, type="double3")
        except RuntimeError:
            om.MGlobal.displayWarning("GRT: Failed to set translate on {}".format(node))
            return
    finally:
        _relock(locked)


# =========================
# Utility: Target Collection
# =========================

def _collect_targets(nodes, hierarchy=False, rig_safe=False, require_opm=False):
    clean_roots = []
    for n in nodes:
        if not _is_transform(n):
            continue
        new_name = _sanitize_fake_namespace(n)
        if new_name is None:
            return []
        clean_roots.append(new_name)

    targets = []
    seen = set()

    for root in clean_roots:
        if hierarchy:
            candidates = _get_children_transforms(root, include_self=True)
        else:
            candidates = [root]

        for c in candidates:
            if c in seen:
                continue
            if not _is_transform(c):
                continue
            if require_opm and not cmds.objExists(f"{c}.offsetParentMatrix"):
                continue
            if rig_safe and not _is_node_rig_safe(c):
                continue

            try:
                _get_dag_path(c)
            except RuntimeError:
                om.MGlobal.displayWarning(f"GRT: Skipping invalid DAG path: {c}")
                continue

            seen.add(c)
            targets.append(c)

    return targets


# =========================
# Utility: Batch Attribute Helpers
# =========================

def _sanitize_label(label):
    # Maya-safe: letters, numbers, spaces, underscores, hyphens
    safe = "".join(c if c.isalnum() or c in " _-" else "_" for c in label)
    return safe.strip()


def _add_divider_attribute(node, name, nice, label):
    """Creates a visual divider enum attribute."""
    if cmds.objExists(f"{node}.{name}"):
        return

    label = _sanitize_label(label)

    cmds.addAttr(node, ln=name, nn=nice, at="enum", en=label)
    cmds.setAttr(f"{node}.{name}", e=True, keyable=False, channelBox=True)
    cmds.setAttr(f"{node}.{name}", lock=True)


def _add_standard_attribute(node, attr_def, is_master):
    name = attr_def["name"]
    nice = attr_def.get("niceName", name)
    attr_type = attr_def["type"]

    # If attribute already exists, skip
    if cmds.objExists(f"{node}.{name}"):
        return

    # Master always gets real attributes
    # Non-master proxy creation is handled in exec_batch_add_attr via evalDeferred

    if attr_type == "float":
        kwargs = {
            "ln": name,
            "nn": nice,
            "at": "double",
            "dv": attr_def.get("default", 0.0),
        }
        if "min" in attr_def and attr_def["min"] is not None:
            kwargs["min"] = attr_def["min"]
        if "max" in attr_def and attr_def["max"] is not None:
            kwargs["max"] = attr_def["max"]
        cmds.addAttr(node, **kwargs)
        cmds.setAttr(f"{node}.{name}", e=True, keyable=True)

    elif attr_type == "enum":
        enum_list = ":".join(attr_def.get("enumNames", []))
        cmds.addAttr(node, ln=name, nn=nice, at="enum", en=enum_list)
        cmds.setAttr(f"{node}.{name}", attr_def.get("default", 0))
        cmds.setAttr(f"{node}.{name}", e=True, keyable=True)

    elif attr_type == "bool":
        cmds.addAttr(node, ln=name, nn=nice, at="bool",
                     dv=attr_def.get("default", False))
        cmds.setAttr(f"{node}.{name}", e=True, keyable=True)


def _apply_proxies_deferred(proxy_ops):
    """
    Runs deferred after exec_batch_add_attr.
    Each entry is (node, master, attrName).
    """
    cmds.undoInfo(openChunk=True, chunkName="GRT_proxyCreation")
    try:
        for node, master, name in proxy_ops:
            # Skip if attribute already exists (Maya would error)
            if cmds.objExists(f"{node}.{name}"):
                continue
            try:
                cmds.addAttr(node, ln=name, proxy=f"{master}.{name}")
            except Exception as e:
                om.MGlobal.displayWarning(
                    f"GRT: Failed to create proxy {node}.{name} -> {master}.{name}: {e}"
                )
    finally:
        cmds.undoInfo(closeChunk=True)


# =========================
# Preset Definitions (built-in)
# =========================

ATTRIBUTE_PRESETS = {
    "FKIK": [
        {"type": "divider", "label": "----------", "name": "FKIKDivider", "nice": "FKIK"},
        {
            "name": "fkikSwitch",
            "niceName": "FKIK Switch",
            "type": "float",
            "default": 0.0,
            "min": 0.0,
            "max": 1.0,
            "proxy": True
        }
    ],

    "SpaceSwap": [
        {"type": "divider", "label": "----------", "name": "SpaceSwapDivider", "nice": "Space Swap"},
        {
            "name": "space",
            "niceName": "Space",
            "type": "enum",
            "enumNames": ["World", "Local", "Head"],
            "default": 0,
            "proxy": True
        }
    ],

    "ParentSwap": [
        {"type": "divider", "label": "----------", "name": "ParentSwapDivider", "nice": "Parent Swap"},
        {
            "name": "parent",
            "niceName": "Parent",
            "type": "enum",
            "enumNames": ["Root", "COG", "Hip"],
            "default": 0,
            "proxy": True
        }
    ]
}


# =========================
# Core Operations
# =========================

def exec_push_opm(nodes, hierarchy=False, rig_safe=False):
    if not nodes:
        om.MGlobal.displayError("GRT_pushOPM: No valid transform selected.")
        return

    cmds.undoInfo(openChunk=True, chunkName="GRT_pushOPM")
    try:
        targets = _collect_targets(nodes, hierarchy=hierarchy, rig_safe=rig_safe, require_opm=True)

        if not targets:
            om.MGlobal.displayError("GRT_pushOPM: No valid transform nodes found in selection.")
            return

        for node in targets:
            try:
                world = _get_world_matrix(node)
                parent_inv = _get_parent_inverse_matrix(node)
                local = world * parent_inv
            except RuntimeError:
                om.MGlobal.displayWarning(f"GRT_pushOPM: Failed to compute matrices for {node}, skipping.")
                continue

            try:
                cmds.setAttr(f"{node}.offsetParentMatrix", *list(local), type="matrix")
                cmds.setAttr(f"{node}.translate", 0, 0, 0, type="double3")
                cmds.setAttr(f"{node}.rotate", 0, 0, 0, type="double3")
                cmds.setAttr(f"{node}.scale", 1, 1, 1, type="double3")
            except RuntimeError:
                om.MGlobal.displayWarning(f"GRT_pushOPM: Failed to write to {node}, skipping.")
                continue
    finally:
        cmds.undoInfo(closeChunk=True)


def exec_pull_opm(nodes, hierarchy=False, rig_safe=False):
    if not nodes:
        om.MGlobal.displayError("GRT_pullOPM: No valid transform selected.")
        return

    cmds.undoInfo(openChunk=True, chunkName="GRT_pullOPM")
    try:
        targets = _collect_targets(
            nodes,
            hierarchy=hierarchy,
            rig_safe=rig_safe,
            require_opm=True
        )

        if not targets:
            om.MGlobal.displayError("GRT_pullOPM: No valid transform nodes found in selection.")
            return

        # Flat identity list, like the old _identity_matrix()
        ident = [
            1.0, 0.0, 0.0, 0.0,
            0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            0.0, 0.0, 0.0, 1.0
        ]

        for node in targets:
            # --- STEP 1: Capture worldMatrix BEFORE touching OPM ---
            try:
                world_before = cmds.getAttr(f"{node}.worldMatrix[0]")
            except RuntimeError:
                om.MGlobal.displayWarning(
                    f"GRT_pullOPM: Failed to read worldMatrix for {node}, skipping."
                )
                continue

            # --- STEP 2: Reset OPM to identity ---
            try:
                cmds.setAttr(f"{node}.offsetParentMatrix", *ident, type="matrix")
            except RuntimeError:
                om.MGlobal.displayWarning(
                    f"GRT_pullOPM: Failed to reset offsetParentMatrix on {node}, skipping."
                )
                continue

            # --- STEP 3: Restore worldMatrix (Maya recomputes TRS) ---
            try:
                cmds.xform(node, matrix=world_before, worldSpace=True)
            except RuntimeError:
                om.MGlobal.displayWarning(
                    f"GRT_pullOPM: Failed to restore worldMatrix on {node}, skipping."
                )
                continue

    finally:
        cmds.undoInfo(closeChunk=True)


def exec_zero_trs(nodes, hierarchy=False, rig_safe=False):
    if not nodes:
        om.MGlobal.displayError("GRT_zeroTRS: No valid transform selected.")
        return

    cmds.undoInfo(openChunk=True, chunkName="GRT_zeroTRS_reverted")
    try:
        targets = _collect_targets(nodes, hierarchy=hierarchy, rig_safe=rig_safe, require_opm=False)

        if not targets:
            om.MGlobal.displayError("GRT_zeroTRS: No valid transform nodes found in selection.")
            return

        for node in targets:
            locked = _unlock_trs(node)
            try:
                cmds.setAttr(f"{node}.translate", 0, 0, 0, type="double3")
                cmds.setAttr(f"{node}.rotate", 0, 0, 0, type="double3")
                cmds.setAttr(f"{node}.scale", 1, 1, 1, type="double3")
            except RuntimeError:
                om.MGlobal.displayWarning(f"GRT_zeroTRS: Failed to zero TRS on {node}, skipping.")
                continue
            finally:
                _relock(locked)
    finally:
        cmds.undoInfo(closeChunk=True)


def exec_auto_grp(nodes):
    if not nodes:
        om.MGlobal.displayError("GRT_autoGRP: No valid transform selected.")
        return

    # Process deeper nodes first
    nodes = sorted(nodes, key=_dag_depth, reverse=True)

    cmds.undoInfo(openChunk=True, chunkName="GRT_autoGRP")
    try:
        valid = [n for n in nodes if _is_transform(n)]
        if not valid:
            om.MGlobal.displayError("GRT_autoGRP: No valid transform nodes found in selection.")
            return

        for node in valid:

            # --- Namespace cleanup ---
            new_name = _sanitize_fake_namespace(node)
            if new_name is None:
                return
            node = new_name

            # --- STEP 0: Normalize node by pulling OPM into TRS ---
            try:
                exec_pull_opm([node], hierarchy=False, rig_safe=False)
            except Exception as e:
                om.MGlobal.displayWarning(f"GRT_autoGRP: Failed to pull OPM on {node}: {e}")

            # --- Parent info ---
            parent = cmds.listRelatives(node, parent=True, fullPath=True)
            parent = parent[0] if parent else None

            # --- Names for new groups ---
            base_name = node.split("|")[-1]
            grp0 = f"{base_name}_0_GRP"
            grpsdk = f"{base_name}_SDK_GRP"

            # --- Name collision skip ---
            if cmds.objExists(grp0) or cmds.objExists(grpsdk):
                om.MGlobal.displayWarning(f"GRT_autoGRP: Groups already exist for {node}, skipping.")
                continue

            # --- STEP 1: Create groups ---
            grp0_node = cmds.createNode("transform", name=grp0)
            grpsdk_node = cmds.createNode("transform", name=grpsdk, parent=grp0_node)

            # --- STEP 2: Snap grp0 to node's worldMatrix ---
            try:
                world = cmds.getAttr(f"{node}.worldMatrix[0]")
                cmds.xform(grp0_node, matrix=world, worldSpace=True)
            except RuntimeError:
                om.MGlobal.displayWarning(f"GRT_autoGRP: Failed to snap {grp0_node} to world, cleaning up.")
                cmds.delete(grp0_node)
                continue

            # --- STEP 3: Parent grp0 under original parent (Maya keeps world) ---
            if parent:
                if cmds.objExists(parent):
                    try:
                        cmds.parent(grp0_node, parent)
                    except RuntimeError:
                        om.MGlobal.displayWarning(
                            f"GRT_autoGRP: Failed to parent {grp0_node} under {parent}, cleaning up."
                        )
                        cmds.delete(grp0_node)
                        continue
                else:
                    om.MGlobal.displayWarning(
                        f"GRT_autoGRP: Original parent {parent} no longer exists for {node}, leaving in world."
                    )

            # --- STEP 4: Parent node under SDK group (Maya keeps world) ---
            try:
                cmds.parent(node, grpsdk_node)
            except RuntimeError:
                om.MGlobal.displayWarning(
                    f"GRT_autoGRP: Failed to parent {node} under {grpsdk_node}, cleaning up."
                )
                cmds.delete(grp0_node)
                continue

    finally:
        cmds.undoInfo(closeChunk=True)


def _mirror_axis_matrix(axis):
    axis = axis.upper()
    if axis == "X":
        return om.MMatrix([
            -1.0, 0.0, 0.0, 0.0,
             0.0, 1.0, 0.0, 0.0,
             0.0, 0.0, 1.0, 0.0,
             0.0, 0.0, 0.0, 1.0
        ])
    if axis == "Y":
        return om.MMatrix([
             1.0, 0.0, 0.0, 0.0,
             0.0,-1.0, 0.0, 0.0,
             0.0, 0.0, 1.0, 0.0,
             0.0, 0.0, 0.0, 1.0
        ])
    if axis == "Z":
        return om.MMatrix([
             1.0, 0.0, 0.0, 0.0,
             0.0, 1.0, 0.0, 0.0,
             0.0, 0.0,-1.0, 0.0,
             0.0, 0.0, 0.0, 1.0
        ])
    raise ValueError(f"Unsupported mirror axis: {axis}")


def _parse_mirror_flags(args):
    syntax = om.MSyntax()
    syntax.addFlag("-a", "-axis", om.MSyntax.kString)
    _add_common_syntax_flags(syntax)

    try:
        arg_data = om.MArgParser(syntax, args)
    except RuntimeError:
        return None, False, False

    axis = "X"
    if arg_data.isFlagSet("-a"):
        axis = arg_data.flagArgumentString("-a", 0).upper()
        if axis not in ("X", "Y", "Z"):
            om.MGlobal.displayError("GRT_mirrorSelection: Axis must be X, Y or Z.")
            return None, False, False

    hierarchy = arg_data.isFlagSet("-h")
    rig_safe = arg_data.isFlagSet("-r")
    return axis, hierarchy, rig_safe


def exec_mirror_selection(nodes, axis="X", hierarchy=True, rig_safe=False):
    if not nodes:
        om.MGlobal.displayError("GRT_mirrorSelection: No valid transform selected.")
        return

    axis = axis.upper() if isinstance(axis, str) else "X"
    if axis not in ("X", "Y", "Z"):
        om.MGlobal.displayError("GRT_mirrorSelection: Axis must be X, Y or Z.")
        return

    targets = _collect_targets(nodes, hierarchy=hierarchy, rig_safe=rig_safe, require_opm=False)
    if not targets:
        om.MGlobal.displayError("GRT_mirrorSelection: No valid transform nodes found in selection.")
        return

    mirror = _mirror_axis_matrix(axis)
    world_matrices = {}
    for node in targets:
        try:
            world_matrices[node] = _get_world_matrix(node)
        except RuntimeError:
            om.MGlobal.displayWarning(f"GRT_mirrorSelection: Failed to read world matrix for {node}, skipping.")

    cmds.undoInfo(openChunk=True, chunkName="GRT_mirrorSelection")
    try:
        for node in sorted(targets, key=_dag_depth):
            if node not in world_matrices:
                continue

            mirrored = mirror * world_matrices[node] * mirror
            locked = _unlock_trs(node)
            try:
                cmds.xform(node, matrix=list(mirrored), worldSpace=True)
            except RuntimeError:
                om.MGlobal.displayWarning(f"GRT_mirrorSelection: Failed to apply mirrored transform to {node}.")
            finally:
                _relock(locked)
    finally:
        cmds.undoInfo(closeChunk=True)


def exec_batch_add_attr(nodes, preset_name, use_proxy=False):
    if not nodes:
        om.MGlobal.displayError("GRT_batchAddAttr: No valid transform selected.")
        return

    if preset_name not in ATTRIBUTE_PRESETS:
        om.MGlobal.displayError(f"GRT_batchAddAttr: Unknown preset '{preset_name}'.")
        return


    cmds.undoInfo(openChunk=True, chunkName="GRT_batchAddAttr")
    try:
        # Use safe target collection, but preserve selection order
        valid = _collect_targets(nodes, hierarchy=False, rig_safe=False, require_opm=False)

        if not valid:
            om.MGlobal.displayError("GRT_batchAddAttr: No valid transform nodes found.")
            return

        master = valid[0]

        # Work on a local copy of the preset to avoid mutating ATTRIBUTE_PRESETS
        # Prefer user override if present
        preset = [dict(a) for a in ATTRIBUTE_PRESETS[preset_name]]


        # Inject master reference for proxy creation (local only)
        for attr in preset:
            attr["_master"] = master

        # Collect proxy operations to run deferred
        proxy_ops = []

        # PASS 1: Create all master attributes first
        for attr in preset:
            if attr["type"] == "divider":
                _add_divider_attribute(master, attr["name"], attr.get("nice", attr["name"]), attr.get("label", "----------"))
            else:
                _add_standard_attribute(master, attr, True)

        # PASS 2: Apply attributes to all other nodes
        for node in valid[1:]:
            for attr in preset:
                if attr["type"] == "divider":
                    _add_divider_attribute(node, attr["name"], attr.get("nice", attr["name"]), attr.get("label", "----------"))
                else:
                    name = attr["name"]

                    if use_proxy and attr.get("proxy", False):
                        # Defer proxy creation
                        proxy_ops.append((node, master, name))
                    else:
                        # Create normal attribute
                        _add_standard_attribute(node, attr, True)

    finally:
        cmds.undoInfo(closeChunk=True)

    # Run proxy creation deferred, outside the undo chunk
    if proxy_ops:
        cmds.evalDeferred(lambda: _apply_proxies_deferred(proxy_ops))


def exec_transfer_proxys(nodes):
    """
    First selected controller becomes master.
    All other selected controllers receive proxy attributes
    for every user-defined attribute on the master.

    Divider attributes ("----------") are transferred as real attributes,
    not proxies.

    Works for ANY user-defined attribute, including those created manually.
    """
    if not nodes or len(nodes) < 2:
        om.MGlobal.displayError("GRT Transfer Proxys: Select at least two controllers.")
        return

    # Validate transforms and sanitize namespaces
    clean = []
    for n in nodes:
        if not _is_transform(n):
            continue
        new_name = _sanitize_fake_namespace(n)
        if new_name is None:
            return
        clean.append(new_name)

    if len(clean) < 2:
        om.MGlobal.displayError("GRT Transfer Proxys: Need at least two valid transform nodes.")
        return

    master = clean[0]
    slaves = clean[1:]

    # Collect all user-defined attributes on master
    user_attrs = cmds.listAttr(master, userDefined=True) or []

    if not user_attrs:
        om.MGlobal.displayWarning(f"GRT Transfer Proxys: Master '{master}' has no user-defined attributes.")
        return

    # Determine which attributes are valid for proxying or divider transfer
    def _get_attr_type(node, attr):
        plug = f"{node}.{attr}"
        try:
            return cmds.getAttr(plug, type=True)
        except Exception:
            return None

    def _is_divider(node, attr):
        """Divider = enum with single value '----------'."""
        plug = f"{node}.{attr}"
        try:
            if cmds.getAttr(plug, type=True) != "enum":
                return False
            enum_names = cmds.attributeQuery(attr, node=node, listEnum=True)[0]
            return isinstance(enum_names, str) and enum_names.strip() == "----------"
        except Exception:
            return False

    def _is_valid_attr(node, attr):
        plug = f"{node}.{attr}"

        if not cmds.objExists(plug):
            return False

        atype = _get_attr_type(node, attr)
        if atype is None:
            return False

        # Skip message attributes
        if atype == "message":
            return False

        # Skip compound parents
        try:
            if cmds.attributeQuery(attr, node=node, numberOfChildren=True) or 0 > 0:
                return False
        except Exception:
            pass

        # Skip multi attributes
        try:
            if cmds.attributeQuery(attr, node=node, multi=True):
                return False
        except Exception:
            pass

        return True

    valid_attrs = [a for a in user_attrs if _is_valid_attr(master, a)]

    if not valid_attrs:
        om.MGlobal.displayWarning(f"GRT Transfer Proxys: No transferable attributes found on '{master}'.")
        return

    # Begin undo chunk
    cmds.undoInfo(openChunk=True, chunkName="GRT_transferProxys")
    try:
        for slave in slaves:
            for attr in valid_attrs:

                # Skip if slave already has the attribute
                if cmds.objExists(f"{slave}.{attr}"):
                    continue

                # Divider → create real divider attribute (with niceName if present)
                if _is_divider(master, attr):
                    try:
                        # Query niceName from master
                        try:
                            nice = cmds.attributeQuery(attr, node=master, niceName=True)
                        except Exception:
                            nice = attr  # fallback

                        # Create divider on slave
                        cmds.addAttr(
                            slave,
                            ln=attr,
                            nn=nice,
                            at="enum",
                            en="----------"
                        )

                        cmds.setAttr(f"{slave}.{attr}", e=True, keyable=False, channelBox=True)
                        cmds.setAttr(f"{slave}.{attr}", lock=True)

                    except Exception as e:
                        om.MGlobal.displayWarning(
                            f"GRT Transfer Proxys: Failed to create divider {slave}.{attr}: {e}"
                        )
                    continue

                # Normal attribute → create proxy
                try:
                    cmds.addAttr(slave, ln=attr, proxy=f"{master}.{attr}")
                except Exception as e:
                    om.MGlobal.displayWarning(
                        f"GRT Transfer Proxys: Failed to create proxy {slave}.{attr} -> {master}.{attr}: {e}"
                    )
                    continue

    finally:
        cmds.undoInfo(closeChunk=True)



# =========================
# MPxCommand shims
# =========================

def _add_common_syntax_flags(syntax):
    syntax.addFlag("-h", "-hierarchy")  # no-arg boolean flag
    syntax.addFlag("-r", "-rigSafe")    # no-arg boolean flag
    return syntax


def _parse_common_flags(args):
    # Build a local syntax matching the flags we actually support
    syntax = om.MSyntax()
    _add_common_syntax_flags(syntax)

    try:
        arg_data = om.MArgParser(syntax, args)
    except RuntimeError:
        # If parsing fails for any reason, fall back to defaults
        return False, False

    hierarchy = arg_data.isFlagSet("-h")
    rig_safe = arg_data.isFlagSet("-r")

    return hierarchy, rig_safe


class GRTPushOPMCommand(om.MPxCommand):
    kCmdName = "GRT_pushOPM"

    @staticmethod
    def creator():
        return GRTPushOPMCommand()

    @staticmethod
    def syntaxCreator():
        syntax = om.MSyntax()
        return _add_common_syntax_flags(syntax)

    def doIt(self, args):
        hierarchy, rig_safe = _parse_common_flags(args)
        sel = cmds.ls(sl=True, long=True) or []
        exec_push_opm(sel, hierarchy=hierarchy, rig_safe=rig_safe)


class GRTPullOPMCommand(om.MPxCommand):
    kCmdName = "GRT_pullOPM"

    @staticmethod
    def creator():
        return GRTPullOPMCommand()

    @staticmethod
    def syntaxCreator():
        syntax = om.MSyntax()
        return _add_common_syntax_flags(syntax)

    def doIt(self, args):
        hierarchy, rig_safe = _parse_common_flags(args)
        sel = cmds.ls(sl=True, long=True) or []
        exec_pull_opm(sel, hierarchy=hierarchy, rig_safe=rig_safe)


class GRTZeroTRSCommand(om.MPxCommand):
    kCmdName = "GRT_zeroTRS"

    @staticmethod
    def creator():
        return GRTZeroTRSCommand()

    @staticmethod
    def syntaxCreator():
        syntax = om.MSyntax()
        return _add_common_syntax_flags(syntax)

    def doIt(self, args):
        hierarchy, rig_safe = _parse_common_flags(args)
        sel = cmds.ls(sl=True, long=True) or []
        exec_zero_trs(sel, hierarchy=hierarchy, rig_safe=rig_safe)


class GRTAutoGRPCommand(om.MPxCommand):
    kCmdName = "GRT_autoGRP"

    @staticmethod
    def creator():
        return GRTAutoGRPCommand()

    @staticmethod
    def syntaxCreator():
        return om.MSyntax()

    def doIt(self, args):
        sel = cmds.ls(sl=True, long=True) or []
        exec_auto_grp(sel)


class GRTMirrorSelectionCommand(om.MPxCommand):
    kCmdName = "GRT_mirrorSelection"

    @staticmethod
    def creator():
        return GRTMirrorSelectionCommand()

    @staticmethod
    def syntaxCreator():
        syntax = om.MSyntax()
        syntax.addFlag("-a", "-axis", om.MSyntax.kString)
        return _add_common_syntax_flags(syntax)

    def doIt(self, args):
        axis, hierarchy, rig_safe = _parse_mirror_flags(args)
        if axis is None:
            return
        sel = cmds.ls(sl=True, long=True) or []
        exec_mirror_selection(sel, axis=axis, hierarchy=hierarchy, rig_safe=rig_safe)


class GRT_SetDisplayOverrideCmd(om.MPxCommand):
    kCmdName = "GRT_setDisplayOverride"

    def __init__(self):
        super(GRT_SetDisplayOverrideCmd, self).__init__()

    def doIt(self, args):
        arg_db = om.MArgParser(self.syntax(), args)

        if not arg_db.isFlagSet("color"):
            om.MGlobal.displayError("Missing -color flag.")
            return

        override_color = arg_db.flagArgumentInt("color", 0)

        sel = cmds.ls(sl=True, long=True) or []
        if not sel:
            om.MGlobal.displayWarning("No objects selected.")
            return

        for obj in sel:
            obj = _normalize_display_node(obj)
            _apply_display_override(obj, override_color)

        om.MGlobal.displayInfo("Applied display override color {}.".format(override_color))

    @staticmethod
    def create_syntax():
        syntax = om.MSyntax()
        syntax.addFlag("c", "color", om.MSyntax.kLong)
        return syntax


def _normalize_display_node(node):
    if not cmds.objExists(node):
        return node
    if cmds.nodeType(node) != "transform":
        parent = cmds.listRelatives(node, parent=True, fullPath=True) or []
        if parent:
            return parent[0]
    return node


def _apply_display_override(node, override_color):
    if not cmds.objExists(node):
        return

    for attr, value in [
        ("overrideEnabled", True),
        ("overrideColor", override_color),
        ("overrideDisplayType", 0),
    ]:
        if cmds.objExists(f"{node}.{attr}"):
            try:
                cmds.setAttr(f"{node}.{attr}", value)
            except Exception as e:
                om.MGlobal.displayWarning(f"Failed to set {node}.{attr}: {e}")


def _reset_display_override(node):
    if not cmds.objExists(node):
        return

    for attr, value in [
        ("overrideEnabled", False),
        ("overrideDisplayType", 0),
        ("overrideColor", 0),
        ("overrideRGBColors", False),
    ]:
        if cmds.objExists(f"{node}.{attr}"):
            try:
                cmds.setAttr(f"{node}.{attr}", value)
            except Exception:
                pass

    for shape in cmds.listRelatives(node, s=True, f=True) or []:
        for attr, value in [
            ("overrideEnabled", False),
            ("overrideDisplayType", 0),
            ("overrideColor", 0),
            ("overrideRGBColors", False),
        ]:
            if cmds.objExists(f"{shape}.{attr}"):
                try:
                    cmds.setAttr(f"{shape}.{attr}", value)
                except Exception:
                    pass


class MatchAndRenameCommand(om.MPxCommand):
    kCmdName = "rigTools_matchAndRename"

    def doIt(self, args):
        sel = cmds.ls(sl=True, long=True)
        if not sel or len(sel) < 2:
            om.MGlobal.displayError("Select at least two objects.")
            return

        driver = sel[-1]
        targets = sel[:-1]

        # Get driver world matrix
        driver_mtx = om.MMatrix(cmds.xform(driver, q=True, ws=True, m=True))

        # Extract base name from driver
        driver_short = driver.split("|")[-1]
        base = self._strip_suffixes(driver_short)

        # Determine numbering rule
        use_numbers = len(targets) > 1

        for i, obj in enumerate(targets, start=1):
            # Apply world matrix
            cmds.xform(obj, ws=True, m=driver_mtx)

            # Build new name
            if use_numbers:
                new_name = f"{base}{i}_CTRL"
            else:
                new_name = f"{base}_CTRL"

            # Rename
            try:
                cmds.rename(obj, new_name)
            except:
                om.MGlobal.displayWarning(f"Could not rename {obj}")

        om.MGlobal.displayInfo("Match and Rename complete.")

    # ----------------------------------------------------------
    # Helper: Strip prefixes and suffixes from driver name
    # ----------------------------------------------------------
    def _strip_suffixes(self, name):
        # Remove side prefixes. Omitted for now to preserve user flexibility.
        #for side in ["L_", "R_", "C_"]:
        #    if name.startswith(side):
        #        name = name[len(side):]

        # Remove known rig suffixes
        suffixes = [
            "_JNT", "_BND", "_BND_JNT", "_CTRL", "_GRP",
            "_SDK", "_SDK_GRP", "_0_GRP"
        ]
        for suf in suffixes:
            if name.endswith(suf):
                name = name[: -len(suf)]

        return name


# =========================
# UI Helpers
# =========================

def _ui_call_push_opm(use_hierarchy, rig_safe):
    args = []
    if use_hierarchy:
        args += ["-h"]
    if rig_safe:
        args += ["-r"]
    cmds.evalDeferred(lambda: cmds.GRT_pushOPM(*args))


def _ui_call_pull_opm(use_hierarchy, rig_safe):
    args = []
    if use_hierarchy:
        args += ["-h"]
    if rig_safe:
        args += ["-r"]
    cmds.evalDeferred(lambda: cmds.GRT_pullOPM(*args))


def _ui_call_zero_trs(use_hierarchy, rig_safe):
    args = []
    if use_hierarchy:
        args += ["-h"]
    if rig_safe:
        args += ["-r"]
    cmds.evalDeferred(lambda: cmds.GRT_zeroTRS(*args))


def _ui_call_set_opm_identity(use_hierarchy, rig_safe):
    sel = cmds.ls(sl=True, long=True) or []
    if not sel:
        om.MGlobal.displayError("GRT: No valid transform selected.")
        return

    targets = _collect_targets(
        sel,
        hierarchy=use_hierarchy,
        rig_safe=rig_safe,
        require_opm=True
    )

    if not targets:
        om.MGlobal.displayError("GRT: No valid transform nodes found.")
        return

    ident = om.MMatrix.kIdentity

    cmds.undoInfo(openChunk=True, chunkName="GRT_setOPMIdentity")
    try:
        for node in targets:
            try:
                cmds.setAttr(f"{node}.offsetParentMatrix", *list(ident), type="matrix")
            except RuntimeError:
                om.MGlobal.displayWarning(f"GRT: Failed to reset offsetParentMatrix on {node}.")
                continue
    finally:
        cmds.undoInfo(closeChunk=True)


def _ui_call_batch_attr(preset, use_proxy):
    sel = cmds.ls(sl=True, long=True) or []
    if not sel:
        om.MGlobal.displayError("GRT Batch Attr: No valid transform selected.")
        return
    exec_batch_add_attr(sel, preset, use_proxy=use_proxy)


def _ui_call_transfer_proxys():
    sel = cmds.ls(sl=True, long=True) or []
    if not sel:
        om.MGlobal.displayError("GRT Transfer Proxys: No valid transform selected.")
        return
    exec_transfer_proxys(sel)


def _ui_call_mirror_selection(axis, use_hierarchy, rig_safe):
    args = ["-a", axis]
    if use_hierarchy:
        args += ["-h"]
    if rig_safe:
        args += ["-r"]
    cmds.evalDeferred(lambda: cmds.GRT_mirrorSelection(*args))


def _ui_call_reset_display_override():
    sel = cmds.ls(sl=True, long=True) or []
    if not sel:
        om.MGlobal.displayError("GRT Display Override: No valid object selected.")
        return

    for obj in sel:
        obj = _normalize_display_node(obj)
        _reset_display_override(obj)

    om.MGlobal.displayInfo("Display override reset.")

# =========================
# UI Wrapper
# =========================

def create_grt_menu():
    if cmds.menu("GRT_RiggingToolsMenu", exists=True):
        cmds.deleteUI("GRT_RiggingToolsMenu")

    cmds.menu(
        "GRT_RiggingToolsMenu",
        label="GRT Rigging Tools",
        parent="MayaWindow",
        tearOff=True
    )

    cmds.menuItem(
        label="OPM Tool Window",
        command=lambda *_: create_grt_opm_window()
    )

    cmds.menuItem(
        label="Auto Group",
        annotation="Creates two groups above the selected object: <name>_0_GRP and <name>_SDK_GRP, matching the object's world transform.",
        command=lambda *_: cmds.GRT_autoGRP()
    )

    cmds.menuItem(
        label="Mirror Selection",
        annotation="Mirror selected controls and groups across a world axis, preserving world position/orientation and hierarchy.",
        command=lambda *_: create_grt_mirror_window()
    )

    cmds.menuItem(
        label="Batch Attribute Tool",
        command=lambda *_: create_grt_batch_attr_window()
    )

    cmds.menuItem(
        label="Display Override Tool",
        command=lambda *_: GRT_showDisplayOverrideUI(),
        annotation="Popup window to set display override colors for selected controls."
    )

    cmds.menuItem(
        label="Match and Rename",
        command=lambda *_: cmds.rigTools_matchAndRename()
    )



def create_grt_opm_window():
    if cmds.window("GRT_OPM_Window", exists=True):
        cmds.deleteUI("GRT_OPM_Window")

    win = cmds.window(
        "GRT_OPM_Window",
        title="GRT Rigging Tools",
        sizeable=True,
        widthHeight=(300, 260)
    )

    cmds.columnLayout(adj=True, rowSpacing=6, columnAlign="center")

    hierarchy_cb = cmds.checkBox(
        label="Apply to Hierarchy",
        value=True,
        annotation="Apply the operation to all selected objects and their children in the hierarchy."
    )

    rigsafe_cb = cmds.checkBox(
        label="Rig-Safe Mode",
        value=True,
        annotation="Skip nodes that are constrained, locked, or non-keyable on TRS channels."
    )

    def _flags():
        use_h = cmds.checkBox(hierarchy_cb, q=True, value=True)
        use_r = cmds.checkBox(rigsafe_cb, q=True, value=True)
        return use_h, use_r

    cmds.separator(height=8, style="in")

    cmds.button(
        label="Push to offsetParentMatrix",
        height=30,
        annotation="Write current Translate/Rotate/Scale into offsetParentMatrix, then zero Translate/Rotate/Scale.",
        command=lambda *_: _ui_call_push_opm(*_flags())
    )

    cmds.button(
        label="Pull from offsetParentMatrix",
        height=30,
        annotation="Extract offsetParentMatrix into Translate/Rotate/Scale, then reset offsetParentMatrix to identity.",
        command=lambda *_: _ui_call_pull_opm(*_flags())
    )

    cmds.separator(height=8, style="in")

    cmds.button(
        label="Zero Transform Attributes (local)",
        height=26,
        annotation="Sets Translation/Rotation/Scale to 0/0/1 without touching offsetParentMatrix.",
        command=lambda *_: _ui_call_zero_trs(*_flags())
    )

    cmds.button(
        label="Set offsetParentMatrix to Identity",
        height=26,
        annotation="Resets offsetParentMatrix to identity without modifying TRS.",
        command=lambda *_: _ui_call_set_opm_identity(*_flags())
    )

    cmds.showWindow(win)


def create_grt_batch_attr_window():
    if cmds.window("GRT_BatchAttr_Window", exists=True):
        cmds.deleteUI("GRT_BatchAttr_Window")

    win = cmds.window(
        "GRT_BatchAttr_Window",
        title="GRT Batch Attribute Tool",
        sizeable=True,
        widthHeight=(360, 360)
    )

    cmds.columnLayout(adj=True, rowSpacing=6, columnAlign="center")

    # Proxy toggle checkbox
    proxy_cb = cmds.checkBox(
        label="Use Proxy Attributes",
        value=True,
        annotation="First controller gets original attributes, others get proxy attributes."
    )

    # Local helper to read checkbox state
    def _proxy():
        return cmds.checkBox(proxy_cb, q=True, value=True)

    cmds.separator(height=8, style="in")

    # Built‑in presets only (no user overrides)
    presets = sorted(ATTRIBUTE_PRESETS.keys())

    for p in presets:
        row = cmds.rowLayout(numberOfColumns=2, adjustableColumn=1, columnAlign=(1, "left"))
        cmds.text(label=p, align="left")
        cmds.button(
            label="Add",
            height=26,
            command=lambda _, name=p: _ui_call_batch_attr(name, _proxy())
        )
        cmds.setParent("..")

    cmds.separator(height=8, style="in")

    cmds.button(
        label="Transfer Proxys",
        height=32,
        annotation=(
            "First selected controller becomes master.\n"
            "All other selected controllers receive proxy attributes\n"
            "for every user-defined attribute on the master."
        ),
        command=lambda *_: _ui_call_transfer_proxys()
    )

    cmds.showWindow(win)


def create_grt_mirror_window():
    if cmds.window("GRT_Mirror_Window", exists=True):
        cmds.deleteUI("GRT_Mirror_Window")

    win = cmds.window(
        "GRT_Mirror_Window",
        title="GRT Mirror Selection",
        sizeable=True,
        widthHeight=(320, 240)
    )

    cmds.columnLayout(adj=True, rowSpacing=6, columnAlign="center")

    hierarchy_cb = cmds.checkBox(
        label="Apply to Hierarchy",
        value=True,
        annotation="Mirror selected objects and their children."
    )

    rigsafe_cb = cmds.checkBox(
        label="Rig-Safe Mode",
        value=True,
        annotation="Skip nodes that are constrained, locked, or non-keyable on TRS channels."
    )

    cmds.text(label="Mirror Axis:", align="left")
    axis_col = cmds.radioCollection()
    cmds.rowLayout(numberOfColumns=3, adjustableColumn=1)
    cmds.radioButton(label="World X", select=True)
    cmds.radioButton(label="World Y")
    cmds.radioButton(label="World Z")
    cmds.setParent("..")

    def _selected_axis():
        selected = cmds.radioCollection(axis_col, q=True, select=True)
        if not selected:
            return "X"
        label = cmds.radioButton(selected, q=True, label=True)
        return label.split()[-1]

    def _flags():
        use_h = cmds.checkBox(hierarchy_cb, q=True, value=True)
        use_r = cmds.checkBox(rigsafe_cb, q=True, value=True)
        return use_h, use_r

    cmds.separator(height=8, style="in")

    cmds.button(
        label="Mirror Selection",
        height=32,
        annotation="Mirror selected transforms across the chosen world axis.",
        command=lambda *_: _ui_call_mirror_selection(_selected_axis(), *_flags())
    )

    cmds.showWindow(win)


def GRT_showDisplayOverrideUI():
    win = "GRT_displayOverrideWin"
    if cmds.window(win, exists=True):
        cmds.deleteUI(win)

    cmds.window(win, title="Set Display Override", sizeable=False)
    col = cmds.columnLayout(adj=True, rs=6, width=180)

    def apply_color(color):
        cmds.GRT_setDisplayOverride(color=color)

    cmds.button(
        label="Left (Green)",
        bgc=(0.3, 0.6, 0.3),
        height=32,
        command=lambda *_: apply_color(14)  # Maya green
    )

    cmds.button(
        label="Right (Red)",
        bgc=(0.6, 0.3, 0.3),
        height=32,
        command=lambda *_: apply_color(13)  # Maya red
    )

    cmds.button(
        label="Center (Yellow)",
        bgc=(0.8, 0.7, 0.2),
        height=32,
        command=lambda *_: apply_color(17)  # Maya yellow
    )

    cmds.button(
        label="General (Light Blue)",
        bgc=(0.4, 0.6, 0.9),
        height=32,
        command=lambda *_: apply_color(18)  # Maya light blue
    )

    cmds.separator(height=8, style="in")

    cmds.button(
        label="Reset Override",
        height=32,
        annotation="Reset display overrides on selected transform and its shape nodes.",
        command=lambda *_: _ui_call_reset_display_override()
    )

    cmds.setParent("..")
    cmds.showWindow(win)


# =========================
# Plugin registration helpers
# =========================

def initializePlugin(mobject):
    mplugin = om.MFnPlugin(mobject, VENDOR, VERSION)
    try:
        mplugin.registerCommand(GRTPushOPMCommand.kCmdName, GRTPushOPMCommand.creator, GRTPushOPMCommand.syntaxCreator)
        mplugin.registerCommand(GRTPullOPMCommand.kCmdName, GRTPullOPMCommand.creator, GRTPullOPMCommand.syntaxCreator)
        mplugin.registerCommand(GRTZeroTRSCommand.kCmdName, GRTZeroTRSCommand.creator, GRTZeroTRSCommand.syntaxCreator)
        mplugin.registerCommand(GRTAutoGRPCommand.kCmdName, GRTAutoGRPCommand.creator, GRTAutoGRPCommand.syntaxCreator)
        mplugin.registerCommand(GRTMirrorSelectionCommand.kCmdName, GRTMirrorSelectionCommand.creator, GRTMirrorSelectionCommand.syntaxCreator)
        mplugin.registerCommand(GRT_SetDisplayOverrideCmd.kCmdName, lambda: GRT_SetDisplayOverrideCmd(), GRT_SetDisplayOverrideCmd.create_syntax)
        mplugin.registerCommand(MatchAndRenameCommand.kCmdName, lambda: MatchAndRenameCommand())


    except Exception as e:
        om.MGlobal.displayError(f"GRT: Failed to register commands: {e}")
    
    cmds.evalDeferred(create_grt_menu)

    om.MGlobal.displayInfo("{} loaded.".format(PLUGIN_NAME))

def uninitializePlugin(mobject):
    mplugin = om.MFnPlugin(mobject)
    try:
        mplugin.deregisterCommand(GRTPushOPMCommand.kCmdName)
        mplugin.deregisterCommand(GRTPullOPMCommand.kCmdName)
        mplugin.deregisterCommand(GRTZeroTRSCommand.kCmdName)
        mplugin.deregisterCommand(GRTAutoGRPCommand.kCmdName)
        mplugin.deregisterCommand(GRTMirrorSelectionCommand.kCmdName)
        mplugin.deregisterCommand(GRT_SetDisplayOverrideCmd.kCmdName)
        mplugin.deregisterCommand(MatchAndRenameCommand.kCmdName)


    except Exception as e:
        om.MGlobal.displayError(f"GRT: Failed to deregister commands: {e}")

    if cmds.menu("GRT_RiggingToolsMenu", exists=True):
        cmds.deleteUI("GRT_RiggingToolsMenu")

    om.MGlobal.displayInfo("{} unloaded.".format(PLUGIN_NAME))
