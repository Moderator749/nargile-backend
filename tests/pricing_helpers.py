"""Saving worksheets in tests, through the calculator's one endpoint, /save.

The old /draft, /file and /apply-to-product endpoints are retired (410). Many
tests were written against them; `legacy_save` sends the same request through
/save — "Yeni Kayıt" or "Üzerine Yaz", never asking about duplicate codes, as
the old endpoints did — and hands back the reply in the old shape: the saved
row for a draft or a file, the row plus what was applied for an apply. The
status code is /save's own (200 on success).
"""

from httpx import AsyncClient, Response

SAVE = "/api/v1/admin/pricing/save"


class Saved:
    """The /save reply, its body in the shape the retired endpoint gave."""

    def __init__(self, resp: Response, kind: str):
        self.status_code = resp.status_code
        self.text = resp.text
        self._resp = resp
        self._kind = kind

    def json(self):
        body = self._resp.json()
        if self.status_code != 200:
            return body
        if self._kind == "apply":
            applied = body["applied"] or {}
            return {"record": body["record"], **applied}
        return body["record"]


async def legacy_save(client: AsyncClient, kind: str, headers: dict, json: dict) -> Saved:
    """`kind`: "draft", "file" or "apply" — what /draft, /file or /apply-to-product did."""
    body: dict = {"sheet": json["sheet"], "apply": kind == "apply"}
    if json.get("replace_id"):
        body |= {"mode": "overwrite", "overwrite_id": json["replace_id"]}
    else:
        body["mode"] = "new"
    if kind == "draft":
        body["place"] = "draft"
    else:
        body["place"] = "file"
        for key in ("file_id", "file_name"):
            if json.get(key) is not None:
                body[key] = json[key]
    return Saved(await client.post(SAVE, headers=headers, json=body), kind)
