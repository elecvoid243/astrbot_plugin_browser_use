"""Authoritative, isolated-world coordinate identity for a private frame tree."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
from uuid import uuid4

from frame_dom import FrameDOM


class CoordinateUnavailable(Exception):
    """The connected frame tree cannot currently authorize new input."""


class FrameCoordinates:
    """Sample frame documents and known scrollers without trusting page globals.

    Events invalidate promptly. Every sample still reads the actual connected
    topology and isolated frame state, so delayed events cannot authorize input.
    This is a coordinate contract, not a general DOM or element semantic lock.
    """

    def __init__(self, browser):
        """Bind identity reads to the actual private browser.

        Args:
            browser: Owned BrowserSession, never selected by an HTTP client.
        """
        self.browser = browser
        self.client = None
        self.target = None
        self.root_session = None
        self.sessions = {}
        self.pending = set()
        self.contexts = {}
        self.world_contexts = {}
        self.world = "astrbot-coordinates-" + uuid4().hex
        self.binding = "astrbot_changed_" + uuid4().hex
        self.key = uuid4().hex
        self.dirty = 0
        self.deliver_events = True
        self.lock = asyncio.Lock()
        self.last_frames = []
        self.last_owners = {}
        self.metrics = []
        self.last_error = None
        self.dom = None
        self.root_objects = {}

    async def close(self):
        """Release only this read-only CDP connection and its isolated contexts."""
        client, self.client = self.client, None
        self.sessions.clear()
        self.pending.clear()
        self.contexts.clear()
        self.world_contexts.clear()
        self.root_objects.clear()
        self.dom = None
        self.target = None
        self.root_session = None
        if client:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.stop(), 2)

    async def _connect(self, target):
        """Follow the current owned tab and recursively attach related OOPIFs.

        Args:
            target: Actual BrowserSession focus target.
        """
        from cdp_use import CDPClient

        await self.close()
        self.target = target
        self.client = client = CDPClient(self.browser.cdp_url)
        dom = self.dom = FrameDOM(client)

        def changed(event, session_id):
            """Invalidate hints without using event data as an identity token.

            Args:
                event: Runtime or Page lifecycle event.
                session_id: Actual source CDP session.
            """
            if self.deliver_events:
                self.dirty += 1

        def attached(event, session_id):
            """Retain related iframe sessions for authoritative child reads.

            Args:
                event: Target attachment from a subscribed owned target.
                session_id: Parent CDP session.
            """
            if event["targetInfo"]["type"] == "iframe":
                self.sessions[event["targetInfo"]["targetId"]] = event["sessionId"]
                self.pending.add(event["sessionId"])
            changed(event, session_id)

        def navigated(event, session_id):
            """Rebuild native node bindings after any owning frame navigation.

            Args:
                event: Browser-supplied frame navigation.
                session_id: Actual source target session.
            """
            dom.drop(session_id)
            changed(event, session_id)

        def detached(event, session_id):
            """Forget disconnected transport contexts before their IDs can recur.

            Args:
                event: Target detachment.
                session_id: Parent CDP session.
            """
            removed = event["sessionId"]
            self.sessions = {
                key: value for key, value in self.sessions.items() if value != removed
            }
            self.contexts = {
                key: value for key, value in self.contexts.items() if key[0] != removed
            }
            self.world_contexts = {
                key: value
                for key, value in self.world_contexts.items()
                if key[0] != removed
            }
            self.root_objects = {
                key: value
                for key, value in self.root_objects.items()
                if key[0] != removed
            }
            self.pending.discard(removed)
            dom.drop(removed)
            changed(event, session_id)

        def context_created(event, session_id):
            """Verify isolated world ownership using browser context metadata.

            Args:
                event: Native execution context description.
                session_id: Source target transport.
            """
            context = event["context"]
            self.world_contexts[(session_id, context["id"])] = context

        def context_destroyed(event, session_id):
            """Discard destroyed world IDs before they can be reused.

            Args:
                event: Native context destruction event.
                session_id: Source target transport.
            """
            cid = event["executionContextId"]
            native = self.world_contexts.pop((session_id, cid), {})
            unique = native.get("uniqueId", event.get("executionContextUniqueId"))
            self.root_objects = {
                key: value
                for key, value in self.root_objects.items()
                if key[:2] != (session_id, unique)
            }
            self.contexts = {
                key: value
                for key, value in self.contexts.items()
                if not (key[0] == session_id and value[0] == cid)
            }
            changed(event, session_id)

        client.register.Target.attachedToTarget(attached)
        client.register.Target.detachedFromTarget(detached)
        client.register.Page.frameAttached(changed)
        client.register.Page.frameDetached(changed)
        client.register.Page.frameNavigated(navigated)
        client.register.Runtime.bindingCalled(changed)

        def contexts_cleared(event, session_id):
            """Invalidate a renderer's world IDs before context numbers can recur.

            Args:
                event: Native context reset notification.
                session_id: Source target transport.
            """
            self.world_contexts = {
                key: value
                for key, value in self.world_contexts.items()
                if key[0] != session_id
            }
            self.root_objects = {
                key: value
                for key, value in self.root_objects.items()
                if key[0] != session_id
            }
            self.contexts = {
                key: value
                for key, value in self.contexts.items()
                if key[0] != session_id
            }
            changed(event, session_id)

        client.register.Runtime.executionContextsCleared(contexts_cleared)
        client.register.Runtime.executionContextCreated(context_created)
        client.register.Runtime.executionContextDestroyed(context_destroyed)
        await asyncio.wait_for(client.start(), 2)
        attached_result = await client.send.Target.attachToTarget(
            params={"targetId": target, "flatten": True}
        )
        self.root_session = attached_result["sessionId"]
        self.sessions[target] = self.root_session
        self.pending.add(self.root_session)

    async def _trees(self):
        """Read current topology from all connected owned target sessions.

        Returns:
            Reachable frame records with their actual owning CDP sessions.

        Raises:
            CoordinateUnavailable: A connected frame is unknown or exceeds a budget.
        """
        while self.pending:
            if len(self.sessions) > 32:
                raise CoordinateUnavailable(
                    "The target inspection budget was exceeded."
                )
            pending = list(self.pending)
            self.pending.clear()
            for sid in pending:
                await asyncio.gather(
                    self.client.send.Runtime.enable(session_id=sid),
                    self.client.send.Page.enable(session_id=sid),
                    self.client.send.Runtime.addBinding(
                        params={
                            "name": self.binding,
                            "executionContextName": self.world,
                        },
                        session_id=sid,
                    ),
                    self.client.send.Target.setAutoAttach(
                        params={
                            "autoAttach": True,
                            "waitForDebuggerOnStart": False,
                            "flatten": True,
                            "filter": [{"type": "iframe", "exclude": False}],
                        },
                        session_id=sid,
                    ),
                )
        sessions = list(set(self.sessions.values()))
        if len(sessions) > 32:
            raise CoordinateUnavailable("The target inspection budget was exceeded.")
        trees = await asyncio.gather(
            *(self.client.send.Page.getFrameTree(session_id=sid) for sid in sessions)
        )
        frames = {}
        root = None
        for sid, tree in zip(sessions, trees):
            stack = [(tree["frameTree"], None)]
            if sid == self.root_session:
                root = tree["frameTree"]["frame"]["id"]
            while stack:
                item, parent = stack.pop()
                frame = dict(item["frame"])
                frame["parentId"] = frame.get("parentId", parent)
                fid = frame["id"]
                # The iframe's own target is authoritative for its loader and
                # children; the embedding target can expose only a placeholder.
                owner = self.sessions.get(fid, sid)
                if fid not in frames or sid == owner:
                    if frame["parentId"] is None and fid in frames:
                        frame["parentId"] = frames[fid]["parentId"]
                    frames[fid] = {**frame, "session": owner}
                    if len(frames) > 64:
                        raise CoordinateUnavailable(
                            "The connected frame budget was exceeded."
                        )
                stack.extend((child, fid) for child in item.get("childFrames", []))
        reachable = {}
        pending = [root]
        while pending:
            fid = pending.pop()
            if fid not in frames:
                raise CoordinateUnavailable("An attached frame is not inspectable.")
            reachable[fid] = frames[fid]
            pending.extend(
                key
                for key, value in frames.items()
                if value["parentId"] == fid and key not in reachable
            )
        if len(reachable) > 32:
            raise CoordinateUnavailable("The frame inspection budget was exceeded.")
        return reachable

    async def _frame(self, frame):
        """Read native coordinates in a private world without scanning each frame.

        Args:
            frame: Authoritative frame ID, loader and owning transport.

        Returns:
            Isolated document identity, viewport, scrollers and transition state.

        Raises:
            CoordinateUnavailable: A native context is unknown, loading or transitioning.
        """
        sid, fid = frame["session"], frame["id"]
        key = (sid, fid, frame.get("loaderId"))
        if len(self.contexts) > 128:
            self.contexts.clear()
        if key not in self.contexts:
            world = await self.client.send.Page.createIsolatedWorld(
                params={"frameId": fid, "worldName": self.world}, session_id=sid
            )
            self.contexts[key] = (world["executionContextId"], uuid4().hex)
        context, nonce = self.contexts[key]
        native = self.world_contexts.get((sid, context), {})
        if (
            native.get("name") != self.world
            or native.get("auxData", {}).get("frameId") != fid
            or not native.get("uniqueId")
        ):
            self.contexts.pop(key, None)
            raise CoordinateUnavailable(
                "The isolated world does not belong to the requested frame."
            )
        roots = sorted(
            (node["backend"], node["shadow"])
            for node in self.dom.roots.get(sid, {}).values()
            if node["frame"] == fid and node["shadow"]
        )
        if len(roots) > 64:
            raise CoordinateUnavailable(
                "The shadow root inspection budget was exceeded."
            )
        arguments = []
        for backend, _ in roots:
            root_key = (sid, native["uniqueId"], backend)
            if root_key not in self.root_objects:
                resolved = await self.client.send.DOM.resolveNode(
                    params={
                        "backendNodeId": backend,
                        "executionContextId": context,
                        "objectGroup": self.world,
                    },
                    session_id=sid,
                )
                reference = resolved.get("object", {}).get("objectId")
                if not reference:
                    raise CoordinateUnavailable(
                        "A native shadow root cannot be inspected."
                    )
                self.root_objects[root_key] = reference
            arguments.append({"objectId": self.root_objects[root_key]})
        removed = [
            key
            for key in self.root_objects
            if key[:2] == (sid, native["uniqueId"])
            and key[2] not in {root[0] for root in roots}
        ]
        for old in removed:
            await self.client.send.Runtime.releaseObject(
                params={"objectId": self.root_objects.pop(old)},
                session_id=sid,
            )
        expression = (
            r"""function(...roots){return JSON.stringify((()=>{
const key=KEY,nonce=NONCE,binding=BINDING;
let s=globalThis[key];
if(!s||s.doc!==document){
 const generation=s?(s.generation+1):0;
 s={doc:document,generation,revision:0,nodes:new Set(),roots:new Set(),ids:new WeakMap(),next:0,scans:0};
 globalThis[key]=s;
 s.track=root=>{const nodes=[root,...(root.querySelectorAll?.('*')||[])];s.scans+=nodes.length;for(const n of nodes){if(!(n instanceof Element))continue;const c=getComputedStyle(n);if([c.overflowX,c.overflowY].some(v=>v!=='visible'&&v!=='clip')){s.nodes.add(n);if(!s.ids.has(n))s.ids.set(n,++s.next)}}};
 s.process=records=>{for(const r of records){if(r.type==='childList'){for(const n of r.addedNodes)s.track(n);let n=r.target;while(n instanceof Element){s.trackOne(n);n=n.parentElement}}else s.trackOne(r.target)}};
 s.trackOne=n=>{if(n instanceof Element){const c=getComputedStyle(n);if([c.overflowX,c.overflowY].some(v=>v!=='visible'&&v!=='clip')){s.nodes.add(n);if(!s.ids.has(n))s.ids.set(n,++s.next)}}};
 s.track(document);
 s.observer=new MutationObserver(s.process);
 s.changed=()=>{s.revision++;try{globalThis[binding]('changed')}catch{}};
 addEventListener('scroll',s.changed,true);addEventListener('resize',s.changed,true);
}
s.process(s.observer.takeRecords());
const next=new Set(roots);
if(roots.some(r=>!r.isConnected||r.ownerDocument!==document))throw Error('A native shadow root is detached or belongs to another document');
if(next.size!==s.roots.size||roots.some(r=>!s.roots.has(r))){
 s.observer.disconnect();
 for(const old of s.roots)if(!next.has(old))old.removeEventListener('scroll',s.changed,true);
 for(const root of roots)if(!s.roots.has(root)){s.track(root);root.addEventListener('scroll',s.changed,true)}
 s.roots=next;s.changed();
}
for(const root of [document,...roots])s.observer.observe(root,{subtree:true,childList:true,attributes:true,attributeFilter:['style','class','hidden']});
const active=document.activeViewTransition;if(active&&active!==s.transition){s.transition=active;s.visual=(s.visual||0)+1}
const scrollers=[];for(const n of s.nodes){if(!n.isConnected){s.nodes.delete(n);continue}scrollers.push([s.ids.get(n),n.scrollLeft,n.scrollTop,n.clientWidth,n.clientHeight])}
if(scrollers.length>512)throw Error('Scroller inspection budget exceeded');
return{document:nonce+':'+s.generation,viewport:[innerWidth,innerHeight,devicePixelRatio],scroll:[scrollX,scrollY],revision:s.revision,visual:s.visual||0,scrollers,transition:!!active,ready:!!document.documentElement&&document.readyState!=='loading',focused:document.hasFocus()&&!(document.activeElement instanceof HTMLIFrameElement),scanned:s.scans};
})())}""".replace("KEY", json.dumps(self.key))
            .replace("NONCE", json.dumps(nonce))
            .replace("BINDING", json.dumps(self.binding))
        )
        # UA-root mode is supplied by native DOM metadata. Reading its .mode
        # getter hung the pinned renderer; only generic Node/Element APIs are used.
        reply = await self.client.send.Runtime.callFunctionOn(
            params={
                "functionDeclaration": expression,
                "executionContextId": context,
                "arguments": arguments,
                "returnByValue": True,
            },
            session_id=sid,
        )
        if reply.get("exceptionDetails") or not reply.get("result", {}).get("value"):
            self.contexts.pop(key, None)
            raise CoordinateUnavailable(
                "An isolated frame is being replaced or is unavailable."
            )
        data = json.loads(reply["result"]["value"])
        if not data["ready"] or data["transition"]:
            raise CoordinateUnavailable(
                "A frame is loading or showing a view transition."
            )
        return {
            "frame": fid,
            "parent": frame["parentId"],
            "loader": frame.get("loaderId"),
            "session": sid,
            "context": context,
            "context_unique": native["uniqueId"],
            "shadow_roots": roots,
            "shadow_revision": self.dom.revision,
            **data,
        }

    async def sample(self, target, *, gesture=False):
        """Authoritatively sample the complete tree in parallel before new input.

        Args:
            target: Actual owned top-level target ID.
            gesture: Validate a held gesture's documents and viewport structure
                without rejecting its own scrolling coordinate changes.

        Returns:
            Opaque full-coordinate and gesture-structure tokens, plus capability.

        Raises:
            CoordinateUnavailable: Topology or a document is unknown or changing.
        """
        async with self.lock:
            started = time.monotonic()
            try:
                if not self.client or self.target != target or not self.root_session:
                    await self._connect(target)
                before = await self._trees()
                sessions = {
                    frame["session"]: frame["id"]
                    for frame in before.values()
                    if frame["parentId"] not in before
                    or before[frame["parentId"]]["session"] != frame["session"]
                }
                await self.dom.sync(sessions)
                shadow_revision = self.dom.revision
                values = await asyncio.gather(
                    *(self._frame(frame) for frame in before.values())
                )
                owners = {}

                async def owner(frame):
                    """Read an embedding frame's native content geometry in parallel.

                    Args:
                        frame: Connected child frame with an authoritative parent.

                    Raises:
                        CoordinateUnavailable: A visible owner has inconsistent geometry.
                    """
                    parent = frame["parentId"]
                    if parent is None:
                        return
                    sid = before[parent]["session"]
                    node = await self.client.send.DOM.getFrameOwner(
                        params={"frameId": frame["id"]}, session_id=sid
                    )
                    try:
                        box = await self.client.send.DOM.getBoxModel(
                            params={"backendNodeId": node["backendNodeId"]},
                            session_id=sid,
                        )
                        owners[frame["id"]] = {
                            "node": node["backendNodeId"],
                            "quad": box["model"]["content"],
                            "size": [box["model"]["width"], box["model"]["height"]],
                        }
                    except Exception:
                        quads = await self.client.send.DOM.getContentQuads(
                            params={"backendNodeId": node["backendNodeId"]},
                            session_id=sid,
                        )
                        if quads.get("quads"):
                            raise CoordinateUnavailable(
                                "A frame owner changed while reading its geometry."
                            ) from None
                        owners[frame["id"]] = {
                            "node": node["backendNodeId"],
                            "quad": [],
                            "size": [0, 0],
                        }

                await asyncio.gather(*(owner(frame) for frame in before.values()))
                after = await self._trees()
                if sorted(
                    (f["id"], f["parentId"] or "", f.get("loaderId"), f["session"])
                    for f in before.values()
                ) != sorted(
                    (f["id"], f["parentId"] or "", f.get("loaderId"), f["session"])
                    for f in after.values()
                ):
                    raise CoordinateUnavailable(
                        "The frame tree changed during the coordinate check."
                    )
                await self.dom.sync(sessions)
                if self.dom.revision != shadow_revision:
                    raise CoordinateUnavailable(
                        "The native shadow tree changed during inspection."
                    )
                repeated = await asyncio.gather(
                    *(self._frame(frame) for frame in after.values())
                )
                stable_fields = (
                    "frame",
                    "parent",
                    "loader",
                    "session",
                    "context",
                    "context_unique",
                    "shadow_roots",
                    "shadow_revision",
                    "document",
                    "viewport",
                    "scroll",
                    "revision",
                    "scrollers",
                    "visual",
                )
                if gesture:
                    stable_fields = tuple(
                        k
                        for k in stable_fields
                        if k not in {"scroll", "revision", "scrollers", "visual"}
                    )
                if sorted(
                    tuple(json.dumps(v[k], sort_keys=True) for k in stable_fields)
                    for v in values
                ) != sorted(
                    tuple(json.dumps(v[k], sort_keys=True) for k in stable_fields)
                    for v in repeated
                ):
                    raise CoordinateUnavailable(
                        "Frame coordinates changed during the authoritative read."
                    )
                original_owners = dict(owners)
                owners.clear()
                await asyncio.gather(*(owner(frame) for frame in after.values()))
                if self.dom.revision != shadow_revision:
                    raise CoordinateUnavailable(
                        "The native shadow tree changed during inspection."
                    )
                if original_owners != owners:
                    raise CoordinateUnavailable(
                        "Frame owner geometry changed during the authoritative read."
                    )
                values.sort(key=lambda f: f["frame"])
                full = []
                structural = []
                for value in values:
                    identity = [
                        value[k]
                        for k in (
                            "frame",
                            "parent",
                            "loader",
                            "session",
                            "context",
                            "context_unique",
                            "shadow_roots",
                            "shadow_revision",
                            "document",
                            "viewport",
                        )
                    ]
                    geometry = owners.get(value["frame"])
                    structural.append(
                        [
                            identity,
                            geometry["node"] if geometry else None,
                            geometry["size"] if geometry else None,
                        ]
                    )
                    full.append(
                        [
                            identity,
                            value["scroll"],
                            value["revision"],
                            value["scrollers"],
                            value["visual"],
                            geometry,
                        ]
                    )
                token = hashlib.sha256(
                    json.dumps(full, separators=(",", ":")).encode()
                ).hexdigest()
                structure = hashlib.sha256(
                    json.dumps(structural, separators=(",", ":")).encode()
                ).hexdigest()
                self.last_frames = values
                self.last_owners = owners
                self.metrics.append(
                    {
                        "ms": (time.monotonic() - started) * 1000,
                        "frames": len(values),
                        "sessions": len(set(v["session"] for v in values)),
                        "scanned": sum(v["scanned"] for v in values),
                        "native_dom_full_scans": self.dom.full_scans,
                        "native_dom_nodes_seen": self.dom.scanned,
                        "shadow_roots": sum(len(v["shadow_roots"]) for v in values),
                    }
                )
                self.metrics = self.metrics[-256:]
                # Removed loaders must not accumulate contexts indefinitely.
                self.contexts = {
                    key: value
                    for key, value in self.contexts.items()
                    if key
                    in {
                        (f["session"], f["id"], f.get("loaderId"))
                        for f in after.values()
                    }
                }
                return {
                    "coordinate_contract_version": 2,
                    "coordinate_ready": not gesture,
                    "gesture_ready": gesture,
                    "coordinate_token": token if not gesture else None,
                    "frame_tree_token": structure,
                    "coordinate_frame_count": len(values),
                    "coordinate_event_revision": self.dirty,
                }
            except CoordinateUnavailable as exc:
                self.last_error = repr(exc)
                raise
            except asyncio.CancelledError:
                # A bounded caller may interrupt attachment or a native read.
                # Rebuild this read-only transport on the next explicit attempt.
                await self.close()
                raise
            except Exception as exc:
                # A lost context is never permission to fall back to top-only
                # validation. The next explicit attempt can rebuild its worlds.
                await self.close()
                self.last_error = repr(exc)
                raise CoordinateUnavailable(
                    "The frame tree is temporarily unavailable."
                ) from exc

    async def route(self, x=None, y=None, *, target=None):
        """Route native input through actual OOPIF owners without selecting DOM elements.

        Args:
            x: Top-viewport pointer X, or None for keyboard focus routing.
            y: Top-viewport pointer Y.
            target: Original gesture's input target; held moves keep that target.

        Returns:
            Owned input target ID and coordinates in that target's viewport.

        Raises:
            CoordinateUnavailable: Native hit testing or an affine owner mapping
                cannot be reconciled with the checked frame tree.
        """
        frames = {f["frame"]: f for f in self.last_frames}
        if x is None:
            focused = [f for f in self.last_frames if f["focused"]]
            sid = focused[-1]["session"] if focused else self.root_session
            return (
                next(key for key, value in self.sessions.items() if value == sid),
                None,
                None,
            )
        sid = self.root_session
        routed = self.target
        for _ in range(8):
            candidates = [
                f
                for f in self.last_frames
                if f["parent"] in frames
                and frames[f["parent"]]["session"] == sid
                and f["session"] != sid
            ]
            if not candidates:
                if target and routed != target:
                    raise CoordinateUnavailable("The gesture input frame was replaced.")
                return routed, x, y
            chosen = None
            if target:
                for frame in candidates:
                    current = frames.get(target)
                    while current:
                        if current["frame"] == frame["frame"]:
                            chosen = frame
                            break
                        current = frames.get(current["parent"])
                    if chosen:
                        break
            else:
                try:
                    hit = await self.client.send.DOM.getNodeForLocation(
                        params={
                            "x": round(x),
                            "y": round(y),
                            "includeUserAgentShadowDOM": True,
                        },
                        session_id=sid,
                    )
                except Exception as exc:
                    raise CoordinateUnavailable(
                        "Native input hit testing is temporarily unavailable."
                    ) from exc
                chosen = next(
                    (
                        f
                        for f in candidates
                        if self.last_owners[f["frame"]]["node"] == hit["backendNodeId"]
                    ),
                    None,
                )
                if chosen is None and candidates:
                    try:
                        node = await self.client.send.DOM.describeNode(
                            params={"backendNodeId": hit["backendNodeId"]},
                            session_id=sid,
                        )
                    except Exception as exc:
                        raise CoordinateUnavailable(
                            "The input frame owner is temporarily unavailable."
                        ) from exc
                    if node["node"]["nodeName"] in {"IFRAME", "FRAME"}:
                        raise CoordinateUnavailable(
                            "An input frame owner changed after validation."
                        )
            if chosen is None:
                if target and routed != target:
                    raise CoordinateUnavailable("The gesture input frame was replaced.")
                return routed, x, y
            quad = self.last_owners[chosen["frame"]]["quad"]
            if len(quad) != 8:
                raise CoordinateUnavailable(
                    "The input frame has no visible content quad."
                )
            ax, ay = quad[2] - quad[0], quad[3] - quad[1]
            bx, by = quad[6] - quad[0], quad[7] - quad[1]
            determinant = ax * by - ay * bx
            if (
                abs(determinant) < 0.001
                or abs(quad[4] - (quad[2] + quad[6] - quad[0])) > 0.1
                or abs(quad[5] - (quad[3] + quad[7] - quad[1])) > 0.1
            ):
                raise CoordinateUnavailable(
                    "The input frame requires an unsupported perspective transform."
                )
            px, py = x - quad[0], y - quad[1]
            u = (px * by - py * bx) / determinant
            v = (ax * py - ay * px) / determinant
            if target is None and not (0 <= u <= 1 and 0 <= v <= 1):
                raise CoordinateUnavailable("The pointer is on a frame border.")
            x, y = u * chosen["viewport"][0], v * chosen["viewport"][1]
            sid = chosen["session"]
            routed = next(key for key, value in self.sessions.items() if value == sid)
            if target == routed:
                return routed, x, y
        raise CoordinateUnavailable("The nested input routing budget was exceeded.")
