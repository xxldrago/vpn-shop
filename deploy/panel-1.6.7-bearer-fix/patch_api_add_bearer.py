"""Compat fix for Amnezia Web Panel 1.6.7 (re-apply after each panel update).

Upstream api_add_user() authorizes via get_current_user() (session cookie
only), so Bearer API tokens get 403 — even though the panel README promises
every session endpoint also accepts tokens. _check_admin() handles both and
returns the same user-record shape, so swapping the resolver keeps the
strict admin-only check intact.
"""
PATH = "/app/app.py"

src = open(PATH, encoding="utf-8").read()
start = src.index("def api_add_user(")
end = src.index("\n@app.", start)
block = src[start:end]

old = """    cur = get_current_user(request)
    if not cur or cur['role'] != 'admin':
        return JSONResponse({'error': 'Forbidden'}, status_code=403)"""
new = """    cur = _check_admin(request)
    if not cur or cur['role'] != 'admin':
        return JSONResponse({'error': 'Forbidden'}, status_code=403)"""

assert block.count(old) == 1, f"expected 1 occurrence in api_add_user, found {block.count(old)}"
src = src[:start] + block.replace(old, new) + src[end:]
open(PATH, "w", encoding="utf-8").write(src)
print("patched api_add_user: Bearer accepted, admin-only kept")
