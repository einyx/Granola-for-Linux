#!/usr/bin/env python3
"""Add Granola's updateHook() to better-sqlite3-multiple-ciphers.

Granola ships a patched fork of better-sqlite3-multiple-ciphers that adds an
updateHook() method. The renderer's cache layer calls it on startup, so a
binary without it makes the app die with:

    TypeError: this[cppdb].updateHook is not a function

Newer Granola builds no longer ship the C++ source in app.asar.unpacked, so
the install script falls back to the npm release. That npm source does not
have the hook, which is exactly why this patch exists.

The hook is implemented on top of SQLite's own sqlite3_update_hook(). The
callback fires for every row that is inserted, updated or deleted, and
receives the operation code, the rowid and the table name.

The addon has two flavours of the same API, so the patch adapts to whichever
one it finds: the V8 based 12.x line that Granola bundles, and the N-API
based 13.x line.

Usage: patch-update-hook.py <path-to-better-sqlite3-multiple-ciphers-src>
"""
import pathlib
import sys

# --- the updateHook implementation -------------------------------------------

CPP_UPDATEHOOK_V8 = """
// The callback that sqlite3_update_hook() invokes. It forwards the operation
// and the rowid to the JavaScript function the user registered.
static void UpdateHookTrampoline(void* context, int operation, char const* database_name, char const* table_name, sqlite3_int64 rowid) {
	((void)database_name);
	((void)table_name);
	Database* db = static_cast<Database*>(context);
	if (!db) return;

	v8::Isolate* isolate = v8::Isolate::GetCurrent();
	if (!isolate) return;

	v8::Local<v8::Value> callback = db->GetUpdateHook().Get(isolate);
	if (callback.IsEmpty() || !callback->IsFunction()) return;

	v8::Local<v8::Context> ctx = isolate->GetCurrentContext();
	if (ctx.IsEmpty()) return;

	v8::Local<v8::Value> args[3];
	args[0] = v8::Number::New(isolate, operation);
	args[1] = v8::Number::New(isolate, static_cast<double>(rowid));
	args[2] = v8::Null(isolate);

	// A throw inside the hook must not take the whole process down, and it
	// must not leave a pending exception behind for the next call.
	v8::TryCatch try_catch(isolate);
	v8::MaybeLocal<v8::Value> result = callback.As<v8::Function>()->Call(ctx, ctx->Global(), 3, args);
	if (result.IsEmpty()) {
		// Swallow the error. The hook is advisory, so a failure in it should
		// not abort the statement that triggered it.
		try_catch.Reset();
	}
}

NODE_METHOD(Database::JS_updateHook) {
	Database* db = Unwrap<Database>(info.This());

	if (info.Length() == 0 || info[0]->IsNull() || info[0]->IsUndefined()) {
		// Passing nothing clears the hook.
		sqlite3_update_hook(db->db_handle, NULL, NULL);
		return;
	}

	REQUIRE_ARGUMENT_ANY(first, v8::Local<v8::Value> callback);
	if (!callback->IsFunction()) {
		ThrowTypeError("updateHook expects a function");
		return;
	}

	db->GetUpdateHook().Reset(OnlyIsolate, callback);

	sqlite3_update_hook(db->db_handle, UpdateHookTrampoline, db);
	return;
}
"""

CPP_UPDATEHOOK_NAPI = """
// The callback that sqlite3_update_hook() invokes. It forwards the operation
// and the rowid to the JavaScript function the user registered.
static void UpdateHookTrampoline(void* context, int operation, char const* database_name, char const* table_name, sqlite3_int64 rowid) {
	((void)database_name);
	((void)table_name);
	Database* db = static_cast<Database*>(context);
	if (!db) return;

	Napi::Reference<Napi::Value>& hook = db->GetUpdateHook();
	if (hook.IsEmpty()) return;

	Napi::Env env = hook.Env();
	if (env.IsExceptionPending()) return;

	Napi::HandleScope scope(env);
	Napi::Value callback = hook.Value();
	if (!callback.IsFunction()) return;

	napi_value args[3];
	args[0] = Napi::Number::New(env, operation);
	args[1] = Napi::Number::New(env, static_cast<double>(rowid));
	args[2] = env.Null();

	// A throw inside the hook must not take the whole process down, and it
	// must not leave a pending exception behind for the next call. SafeCall
	// clears the pending exception and returns an empty value on failure.
	SafeCall(env, Napi::Function(env, callback), env.Undefined(), 3, args);
}

NODE_METHOD(Database::JS_updateHook) {
	UNWRAP_OR_RETURN(Database, db, info.This());

	if (info.Length() == 0 || info[0].IsNull() || info[0].IsUndefined()) {
		// Passing nothing clears the hook.
		if (!db->GetUpdateHook().IsEmpty()) db->GetUpdateHook().Reset();
		sqlite3_update_hook(db->db_handle, NULL, NULL);
		return info.Env().Undefined();
	}

	REQUIRE_ARGUMENT_ANY(first, Napi::Value callback);
	if (!callback.IsFunction()) {
		Napi::TypeError::New(info.Env(), "updateHook expects a function").ThrowAsJavaScriptException();
		return info.Env().Undefined();
	}

	if (!db->GetUpdateHook().IsEmpty()) db->GetUpdateHook().Reset();
	db->GetUpdateHook() = Napi::Persistent(callback);
	db->GetUpdateHook().SuppressDestruct();

	sqlite3_update_hook(db->db_handle, UpdateHookTrampoline, db);
	return info.Env().Undefined();
}
"""

# --- anchors -----------------------------------------------------------------

# The 12.x line unwraps the database with Unwrap<Database>(info.This()), the
# 13.x line uses the UNWRAP_OR_RETURN macro. Anchor the insertion on the end
# of JS_unsafeMode rather than on its exact body.
V8_ANCHOR = """	sqlite3_db_config(db->db_handle, SQLITE_DBCONFIG_DEFENSIVE, static_cast<int>(!db->unsafe_mode), NULL);
}
"""

NAPI_ANCHOR = """	sqlite3_db_config(db->db_handle, SQLITE_DBCONFIG_DEFENSIVE, static_cast<int>(!db->unsafe_mode), NULL);
	return info.Env().Undefined();
}
"""

# --- Electron 45 / V8 13 fixes ------------------------------------------------

CPP_OLD_EXTERNAL = """	v8::Local<v8::External> data = v8::External::New(isolate, addon);"""

CPP_NEW_EXTERNAL = """	v8::Local<v8::External> data = v8::External::New(isolate, addon, v8::kExternalPointerTypeTagDefault);"""

# V8 13 changed External::Value() to require the type tag as well.
CPP_OLD_ONLYADDON = """#define OnlyAddon static_cast<Addon*>(info.Data().As<v8::External>()->Value())"""

CPP_NEW_ONLYADDON = """#define OnlyAddon static_cast<Addon*>(info.Data().As<v8::External>()->Value(v8::kExternalPointerTypeTagDefault))"""

# Electron 45 ships V8 13, which brought GetPrototypeV2 and HolderV2 back.
MACROS_OLD_PROTOTYPE = """#if defined(V8_MAJOR_VERSION) && V8_MAJOR_VERSION >= 13
// v8::Object::GetPrototype has been deprecated. See http://crbug.com/333672197
#define GET_PROTOTYPE(obj) ((obj)->GetPrototypeV2())
#else
#define GET_PROTOTYPE(obj) ((obj)->GetPrototype())
#endif"""

MACROS_NEW_PROTOTYPE = """#if defined(V8_MAJOR_VERSION) && V8_MAJOR_VERSION >= 13
// Electron 45+ exposes GetPrototype() again.
#define GET_PROTOTYPE(obj) ((obj)->GetPrototype())
#else
#define GET_PROTOTYPE(obj) ((obj)->GetPrototype())
#endif"""

MACROS_OLD_HOLDER = """#if defined(V8_MAJOR_VERSION) && V8_MAJOR_VERSION >= 13
#define PROPERTY_HOLDER(info) (info).HolderV2()
#else
#define PROPERTY_HOLDER(info) (info).This()
#endif"""

MACROS_NEW_HOLDER = """#if defined(V8_MAJOR_VERSION) && V8_MAJOR_VERSION >= 13
#define PROPERTY_HOLDER(info) (info).Holder()
#else
#define PROPERTY_HOLDER(info) (info).This()
#endif"""


def patch(path: pathlib.Path, old: str, new: str) -> bool:
    """Replace old with new. Returns True when something changed."""
    if not path.exists():
        return False
    text = path.read_text()
    if new in text:
        return False
    if old not in text:
        return False
    path.write_text(text.replace(old, new, 1))
    return True


def patch_v8(root: pathlib.Path) -> int:
    """Patch the V8 based 12.x line of the addon."""
    hpp = root / "src" / "objects" / "database.hpp"
    cpp = root / "src" / "objects" / "database.cpp"

    changes = 0
    changes += patch(hpp, """	static NODE_METHOD(JS_defaultSafeIntegers);
	static NODE_METHOD(JS_unsafeMode);""", """	static NODE_METHOD(JS_defaultSafeIntegers);
	static NODE_METHOD(JS_unsafeMode);
	static NODE_METHOD(JS_updateHook);""")
    changes += patch(hpp, """	const v8::Global<v8::Value> logger;""", """	const v8::Global<v8::Value> logger;
	v8::Global<v8::Value> update_hook;""")
    changes += patch(hpp, """	inline State* GetState() { return reinterpret_cast<State*>(&open); }
	inline sqlite3* GetHandle() { return db_handle; }
	inline Addon* GetAddon() { return addon; }""", """	inline State* GetState() { return reinterpret_cast<State*>(&open); }
	inline sqlite3* GetHandle() { return db_handle; }
	inline Addon* GetAddon() { return addon; }

	// The update hook is invoked from a C callback, so the reference to the
	// JavaScript function has to be reachable from outside the class.
	inline v8::Global<v8::Value>& GetUpdateHook() { return update_hook; }""")
    changes += patch(cpp, """	SetPrototypeMethod(isolate, data, t, "unsafeMode", JS_unsafeMode);""", """	SetPrototypeMethod(isolate, data, t, "unsafeMode", JS_unsafeMode);
	SetPrototypeMethod(isolate, data, t, "updateHook", JS_updateHook);""")
    changes += patch(cpp, """	logger(isolate, logger),
	stmts(),
	backups() {""", """	logger(isolate, logger),
	update_hook(),
	stmts(),
	backups() {""")
    changes += patch(cpp, V8_ANCHOR, V8_ANCHOR + CPP_UPDATEHOOK_V8)
    return changes


def patch_napi(root: pathlib.Path) -> int:
    """Patch the N-API based 13.x line of the addon."""
    hpp = root / "src" / "objects" / "database.hpp"
    cpp = root / "src" / "objects" / "database.cpp"

    changes = 0
    changes += patch(hpp, """	static NODE_METHOD(JS_defaultSafeIntegers);
	static NODE_METHOD(JS_unsafeMode);
	static NODE_GETTER(JS_open);""", """	static NODE_METHOD(JS_defaultSafeIntegers);
	static NODE_METHOD(JS_unsafeMode);
	static NODE_METHOD(JS_updateHook);
	static NODE_GETTER(JS_open);""")
    changes += patch(hpp, """	Napi::Reference<Napi::Value> logger;
	std::set<Statement*, CompareStatement> stmts;""", """	Napi::Reference<Napi::Value> logger;
	Napi::Reference<Napi::Value> update_hook;
	std::set<Statement*, CompareStatement> stmts;""")
    changes += patch(hpp, """	inline State* GetState() { return reinterpret_cast<State*>(&open); }
	inline sqlite3* GetHandle() { return db_handle; }
	inline Addon* GetAddon() { return addon; }""", """	inline State* GetState() { return reinterpret_cast<State*>(&open); }
	inline sqlite3* GetHandle() { return db_handle; }
	inline Addon* GetAddon() { return addon; }

	// The update hook is invoked from a C callback, so the reference to the
	// JavaScript function has to be reachable from outside the class.
	inline Napi::Reference<Napi::Value>& GetUpdateHook() { return update_hook; }""")
    changes += patch(cpp, """		PrototypeMethod<Database, &Database::JS_unsafeMode>("unsafeMode", addon),
	}, addon);""", """		PrototypeMethod<Database, &Database::JS_unsafeMode>("unsafeMode", addon),
		PrototypeMethod<Database, &Database::JS_updateHook>("updateHook", addon),
	}, addon);""")
    changes += patch(cpp, NAPI_ANCHOR, NAPI_ANCHOR + CPP_UPDATEHOOK_NAPI)
    return changes


def patch_electron_45(root: pathlib.Path) -> int:
    """Apply the V8 13 fixes that Electron 45 needs."""
    changes = 0
    changes += patch(root / "src" / "better_sqlite3.cpp", CPP_OLD_EXTERNAL, CPP_NEW_EXTERNAL)
    macros = root / "src" / "util" / "macros.cpp"
    changes += patch(macros, CPP_OLD_ONLYADDON, CPP_NEW_ONLYADDON)
    changes += patch(macros, MACROS_OLD_PROTOTYPE, MACROS_NEW_PROTOTYPE)
    changes += patch(macros, MACROS_OLD_HOLDER, MACROS_NEW_HOLDER)
    return changes


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2

    root = pathlib.Path(sys.argv[1])
    hpp = root / "src" / "objects" / "database.hpp"
    cpp = root / "src" / "objects" / "database.cpp"

    if not cpp.exists():
        print(f"no database.cpp under {root}", file=sys.stderr)
        return 1

    hpp_text = hpp.read_text() if hpp.exists() else ""
    is_napi = "Napi::ObjectWrap" in hpp_text

    changes = 0
    if is_napi:
        changes += patch_napi(root)
    else:
        changes += patch_v8(root)
    changes += patch_electron_45(root)

    if changes == 0:
        print("updateHook is already present, nothing to do")
        return 0

    flavour = "N-API" if is_napi else "V8"
    print(f"patched updateHook into {root} ({flavour}, {changes} edit(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
