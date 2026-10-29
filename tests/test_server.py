"""The local app must refuse requests that other websites could make."""
import http.client


def req(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port)
    c.request(method, path, body=body, headers={"Host": f"127.0.0.1:{port}", **(headers or {})})
    return c.getresponse().status


def test_local_requests_work(app):
    assert req(app, "GET", "/api/library") == 200


def test_foreign_host_origin_and_form_posts_are_refused(app):
    assert req(app, "GET", "/api/library", headers={"Host": "evil.example"}) == 403
    assert req(app, "POST", "/api/tasks", b"kind=ask", {"Content-Type": "application/x-www-form-urlencoded"}) == 403
    assert req(app, "POST", "/api/tasks", b'{"kind":"ask","question":"x"}',
               {"Content-Type": "application/json", "Origin": "https://evil.example"}) == 403


def test_bad_task_requests_fail_fast(app):
    json_ct = {"Content-Type": "application/json"}
    assert req(app, "POST", "/api/tasks", b'{"kind":"discover","searches":[]}', json_ct) == 400
    assert req(app, "POST", "/api/tasks", b'{"kind":"nope"}', json_ct) == 400


def test_ui_modules_are_served_and_paths_are_strict(app):
    assert req(app, "GET", "/") == 200 and req(app, "GET", "/app.css") == 200
    assert req(app, "GET", "/js/main.js") == 200
    for bad in ("/js/../server.py", "/js/%2e%2e/server.py", "/js/Main.js", "/js/nope.js", "/app.js", "/js/main.js.map"):
        assert req(app, "GET", bad) == 404, bad


def test_queued_videos_keep_the_titles_they_were_sent_with(app):
    body = b'{"text": "AAAAAAAAAA1", "titles": {"AAAAAAAAAA1": "A real title"}}'
    assert req(app, "POST", "/api/jobs", body, {"Content-Type": "application/json"}) == 200
    c = http.client.HTTPConnection("127.0.0.1", app)
    c.request("GET", "/api/jobs", headers={"Host": f"127.0.0.1:{app}"})
    import json
    jobs = json.loads(c.getresponse().read())
    assert any(j["vid"] == "AAAAAAAAAA1" and j["title"] == "A real title" for j in jobs)


def test_finished_non_english_video_is_requeued_only_for_translation(app):
    from tkb import config
    from tkb.core.storage import write_json
    for vid, lang in (("HHHHHHHHHH1", "hi"), ("EEEEEEEEEE1", "en")):
        d = config.DATA / vid
        d.mkdir(parents=True)
        write_json(d / "kb.json", {"concepts": []})
        write_json(d / "transcript.json", {"language": lang, "task": "transcribe"})
    ct = {"Content-Type": "application/json"}
    import json

    def post(body):
        c = http.client.HTTPConnection("127.0.0.1", app)
        c.request("POST", "/api/jobs", body=json.dumps(body), headers={"Host": f"127.0.0.1:{app}", **ct})
        return json.loads(c.getresponse().read())

    assert post({"text": "HHHHHHHHHH1 EEEEEEEEEE1"}) == {"added": 0, "skipped": 2}
    assert post({"text": "HHHHHHHHHH1 EEEEEEEEEE1", "translate": True}) == {"added": 1, "skipped": 1}



def test_the_settings_page_reads_and_saves(app):
    import json
    c = http.client.HTTPConnection("127.0.0.1", app)
    c.request("GET", "/api/settings", headers={"Host": f"127.0.0.1:{app}"})
    page = json.loads(c.getresponse().read())
    assert [s["id"] for s in page["sections"]][:3] == ["models", "providers", "noise"] and page["about"]
    json_ct = {"Content-Type": "application/json"}
    assert req(app, "POST", "/api/settings", b'{"values": {"jobs": 0}}', json_ct) == 400
    assert req(app, "POST", "/api/settings", b'{"values": {"jobs": 3}}', json_ct) == 200
    from tkb import config
    assert config.get("jobs") == 3
