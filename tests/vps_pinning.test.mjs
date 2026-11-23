import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../web/vps.js", import.meta.url), "utf8");
function load(storage = new Map(), options = {}) {
  const context = vm.createContext({
    S: {
      session: { user_id: "u" },
      space_id: "space",
      page: "vps",
      renderSeq: 1,
      projects: [],
    },
    document: { addEventListener() {} },
    window: {},
    esc: String,
    icon: () => "",
    heading: () => "",
    loadBasics: async () => {},
    localStorage: {
      getItem: (key) => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, value),
      removeItem: (key) => storage.delete(key),
    },
    ...options,
  });
  vm.runInContext(source, context);
  return context;
}
const record = (id) => ({
  id,
  name: id,
  host: "example.invalid",
  enabled: true,
  project_ids: [],
});

test("pins only reorder server-returned inventory, even across pages", async () => {
  const calls = [];
  const panel = load(new Map(), {
    api: async (path) => {
      calls.push(path);
      if (!path.startsWith("/api/vps?")) return { sessions: [] };
      if (path.endsWith("offset=0"))
        return { vps: [record("a"), record("b")], next_offset: 2 };
      return { vps: [record("c"), record("d")], next_offset: null };
    },
  });
  panel.vpsWritePins(panel.vpsPinKey(), new Set(["c", "missing", "a"]));
  const html = await panel.vpsHTML(1);
  assert.match(
    html,
    /data-vps-card="a".*data-vps-card="c".*data-vps-card="b".*data-vps-card="d"/s,
  );
  assert.doesNotMatch(html, /data-vps-card="missing"/);
  assert.match(html, /aria-label="置顶 c" aria-pressed="true"/);
  assert.deepEqual(
    calls.filter((s) => s.startsWith("/api/vps?")),
    ["/api/vps?limit=200&offset=0", "/api/vps?limit=200&offset=2"],
  );
});
test("refresh and account or Space changes are isolated; removing last pin clears storage", () => {
  const storage = new Map(),
    original = load(storage);
  original.vpsWritePins(original.vpsPinKey(), new Set(["vps-id"]));
  assert.deepEqual([...storage.values()], ['["vps-id"]']);
  const fresh = load(storage);
  assert.equal(fresh.vpsReadPins().has("vps-id"), true);
  fresh.S.space_id = "other";
  assert.equal(fresh.vpsReadPins().size, 0);
  fresh.S.space_id = "space";
  fresh.S.session.user_id = "other";
  assert.equal(fresh.vpsReadPins().size, 0);
  fresh.S.session = null;
  assert.equal(fresh.vpsPinKey(), null);
  fresh.S.session = { user_id: "u" };
  fresh.vpsWritePins(fresh.vpsPinKey(), new Set());
  assert.equal(storage.size, 0);
});
test("corrupt storage and rejected writes still permit current-session favorites", () => {
  const panel = load(new Map(), {
    localStorage: {
      getItem: () => "not valid JSON",
      setItem: () => {
        throw new Error("disabled");
      },
    },
  });
  assert.equal(panel.vpsReadPins().size, 0);
  panel.vpsWritePins(panel.vpsPinKey(), new Set(["local"]));
  assert.equal(panel.vpsReadPins().has("local"), true);
  panel.S.space_id = "other";
  assert.equal(panel.vpsReadPins().size, 0);
});
