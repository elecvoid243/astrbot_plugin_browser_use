"""Maintain the native DOM inventory used to discover closed shadow roots."""

from __future__ import annotations

import asyncio


class FrameDOM:
    """Follow native DOM identities and expand only newly inserted subtrees.

    The pinned CDP client awaits each event handler before processing the next
    WebSocket message. Synchronous inventory updates plus a native DOM round trip
    therefore precede its response. Binding events remain optional hints; raw
    scroll positions are always read separately in verified isolated contexts.
    """

    def __init__(self, client):
        """Register synchronous lifecycle handlers on the private CDP reader.

        Args:
            client: Independent coordinate-reader CDP connection.
        """
        self.client = client
        self.nodes = {}
        self.roots = {}
        self.documents = {}
        self.pending = {}
        self.revision = 0
        self.full_scans = 0
        self.scanned = 0
        self.error = None

        def updated(event, session_id):
            """Discard invalid frontend node IDs after a native document reset.

            Args:
                event: Native document reset notification.
                session_id: Actual owning target session.
            """
            self.drop(session_id)

        def inserted(event, session_id):
            """Ingest a native inserted node or newly attached shadow root.

            Args:
                event: Native child or shadow-root insertion.
                session_id: Actual owning target session.
            """
            parent = event.get("parentNodeId", event.get("hostId"))
            node = event.get("node", event.get("root"))
            owner = self.nodes.get(session_id, {}).get(parent)
            if owner is None:
                self.drop(session_id)
                return
            self.ingest(session_id, node, parent, owner["frame"])

        def removed(event, session_id):
            """Forget removed native subtrees and their shadow identities.

            Args:
                event: Native child or shadow-root removal.
                session_id: Actual owning target session.
            """
            self.remove(session_id, event.get("nodeId", event.get("rootId")))

        def children(event, session_id):
            """Apply an authoritative requested child inventory without rescanning.

            Args:
                event: Native setChildNodes response event.
                session_id: Actual owning target session.
            """
            parent = event["parentId"]
            owner = self.nodes.get(session_id, {}).get(parent)
            if owner is None:
                self.drop(session_id)
                return
            present = {node["nodeId"] for node in event["nodes"]}
            for previous in list(owner["children"]):
                # setChildNodes describes light children, not the owner's shadow.
                old = self.nodes[session_id].get(previous)
                if old and not old["shadow"] and previous not in present:
                    self.remove(session_id, previous)
            for node in event["nodes"]:
                self.ingest(session_id, node, parent, owner["frame"])

        def count_changed(event, session_id):
            """Expand children that were not previously requested by the frontend.

            Args:
                event: Native child-count update for an unexpanded node.
                session_id: Actual owning target session.
            """
            self.pending.setdefault(session_id, set()).add(event["nodeId"])

        client.register.DOM.documentUpdated(updated)
        client.register.DOM.childNodeInserted(inserted)
        client.register.DOM.childNodeRemoved(removed)
        client.register.DOM.setChildNodes(children)
        client.register.DOM.childNodeCountUpdated(count_changed)
        client.register.DOM.shadowRootPushed(inserted)
        client.register.DOM.shadowRootPopped(removed)

    def drop(self, session_id):
        """Invalidate a target inventory so its next read rebuilds it natively.

        Args:
            session_id: Disconnected or reset native target session.
        """
        if any(n["shadow"] for n in self.nodes.get(session_id, {}).values()):
            self.revision += 1
        self.nodes.pop(session_id, None)
        self.roots.pop(session_id, None)
        self.documents.pop(session_id, None)
        self.pending.pop(session_id, None)

    def remove(self, session_id, node_id):
        """Remove a subtree using known native parent/child identities.

        Args:
            session_id: Actual native target session.
            node_id: Removed native frontend node ID.
        """
        nodes = self.nodes.get(session_id, {})
        stack = [node_id]
        while stack:
            node = nodes.pop(stack.pop(), None)
            if not node:
                continue
            if node["shadow"]:
                self.revision += 1
                self.roots.get(session_id, {}).pop(node["id"], None)
            parent = nodes.get(node["parent"])
            if parent:
                parent["children"].discard(node["id"])
            self.pending.get(session_id, set()).discard(node["id"])
            stack.extend(node["children"])

    def ingest(self, session_id, node, parent=None, frame=None):
        """Index native node ownership and discover open, closed and UA roots.

        Args:
            session_id: Actual native target session.
            node: Browser-supplied DOM node and optional descendants.
            parent: Native embedding node ID.
            frame: Owning document's native frame ID.
        """
        nodes = self.nodes.setdefault(session_id, {})
        pending = self.pending.setdefault(session_id, set())
        stack = [(node, parent, frame)]
        while stack:
            current, parent, frame = stack.pop()
            if current["nodeType"] == 9:
                frame = current.get("frameId", frame)
            nid = current["nodeId"]
            old = nodes.get(nid)
            shadow = current.get("shadowRootType")
            if not old:
                self.scanned += 1
                if shadow:
                    self.revision += 1
            record = nodes[nid] = {
                "id": nid,
                "backend": current["backendNodeId"],
                "parent": parent,
                "frame": frame,
                "shadow": shadow,
                "children": old["children"] if old else set(),
            }
            if shadow:
                self.roots.setdefault(session_id, {})[nid] = record
            if parent in nodes:
                nodes[parent]["children"].add(nid)
            if len(nodes) > 20000:
                self.error = "The native DOM inspection budget was exceeded."
                return
            if current.get("childNodeCount", 0) and "children" not in current:
                pending.add(nid)
            if "children" in current:
                pending.discard(nid)
            stack.extend((child, nid, frame) for child in current.get("children", []))
            stack.extend((root, nid, frame) for root in current.get("shadowRoots", []))
            if current.get("contentDocument"):
                stack.append((current["contentDocument"], nid, current.get("frameId")))
            if shadow and not record["frame"]:
                self.error = "A native shadow root has no verified owning frame."

    async def sync(self, sessions):
        """Confirm native document ownership and flush ordered lifecycle changes.

        Args:
            sessions: Native target sessions mapped to their actual root frame IDs.

        Raises:
            ValueError: The native inventory is incomplete, reset or over budget.
        """
        for sid in list(self.nodes):
            if sid not in sessions:
                self.drop(sid)

        async def document(sid):
            """Initialize a target once, otherwise issue a constant-depth barrier.

            Args:
                sid: Actual owning target session.

            Raises:
                ValueError: A document reset invalidates the current inventory.
            """
            initial = sid not in self.documents
            if initial:
                await self.client.send.DOM.enable(session_id=sid)
                reply = await self.client.send.DOM.getDocument(
                    params={"depth": -1, "pierce": True}, session_id=sid
                )
                root = reply["root"]
            else:
                # getDocument resets frontend node bindings even at depth zero.
                # describeNode preserves the lifecycle subscription inventory.
                reply = await self.client.send.DOM.describeNode(
                    params={
                        "nodeId": self.documents[sid][0],
                        "depth": 0,
                        "pierce": True,
                    },
                    session_id=sid,
                )
                root = reply["node"]
            identity = (root["nodeId"], root["backendNodeId"])
            if initial:
                self.ingest(sid, root, frame=sessions[sid])
                self.documents[sid] = identity
                self.full_scans += 1
            elif self.documents.get(sid) != identity:
                self.drop(sid)
                raise ValueError("The native DOM document changed during its barrier.")

        await asyncio.gather(*(document(sid) for sid in sessions))
        for _ in range(4):
            pending = [
                (sid, node) for sid in sessions for node in self.pending.get(sid, ())
            ]
            if not pending:
                break
            if len(pending) > 64:
                for sid in sessions:
                    self.drop(sid)
                raise ValueError("Native subtree expansion needs a fresh inventory.")
            for sid, node in pending:
                self.pending[sid].discard(node)
            await asyncio.gather(
                *(
                    self.client.send.DOM.requestChildNodes(
                        params={"nodeId": node, "depth": -1, "pierce": True},
                        session_id=sid,
                    )
                    for sid, node in pending
                )
            )
        if self.error or any(self.pending.get(sid) for sid in sessions):
            raise ValueError(
                self.error or "The native shadow inventory is still changing."
            )
