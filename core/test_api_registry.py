"""Unified REST contract, schema validation, token scope and tenancy boundaries."""

from datetime import timedelta
from unittest import mock

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.api.context import CallContext
from core.api.errors import ApiError
from core.api.registry import Endpoint, Registry, endpoint, registry
from core.api.schema import check_schema, loads, validate
from core.forms import APITokenForm
from core.models import APIToken, Brain, User, Workspace
from core.test_plugin_api import _mock_setup

INPUT = {
    "type": "object",
    "properties": {"n": {"type": "integer", "minimum": 1, "maximum": 4}},
    "required": ["n"],
    "additionalProperties": False,
}


class SchemaTests(SimpleTestCase):
    def test_type_range_required_and_additional_properties(self):
        for value in ({}, {"n": True}, {"n": 0}, {"n": 5}, {"n": "2"}, {"n": 2, "x": 3}, []):
            with self.subTest(value=value), self.assertRaises(ApiError):
                validate(value, INPUT)
        self.assertEqual(validate({"n": 2}, INPUT), {"n": 2})

    def test_nested_array_enum_lengths_and_nullable(self):
        schema = {
            "type": "array",
            "maxItems": 2,
            "items": {"type": ["string", "null"], "maxLength": 3, "enum": ["yes", None]},
        }
        check_schema(schema)
        self.assertEqual(validate(["yes", None], schema), ["yes", None])
        for value in (["no"], ["long"], [True], [None] * 3):
            with self.subTest(value=value), self.assertRaises(ApiError):
                validate(value, schema)
        with self.assertRaises(ApiError):
            validate(True, {"type": "boolean", "enum": [1]})

    def test_json_rejects_nonfinite_and_malformed_values(self):
        for value in ("NaN", "Infinity", "-Infinity", "{", b"\xff"):
            with self.subTest(value=value), self.assertRaises(ApiError):
                loads(value)
        with self.assertRaises(ApiError):
            validate(loads("1e999"), {"type": "number"})

    def test_unknown_schema_features_are_not_silently_ignored(self):
        for schema in (
            {"type": "string", "pattern": "foo"},
            {"type": "array"},
            {"type": "object", "required": ["x"]},
            {"type": "garbage"},
        ):
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                check_schema(schema)


class RegistryTests(SimpleTestCase):
    def test_duplicate_mcp_name_and_invalid_registration(self):
        r = Registry()
        spec = Endpoint(
            "test.note", lambda ctx, p: p, INPUT, "Note", "", "read", "brain:read", (60, 60)
        )
        r.register(spec)
        with self.assertRaises(ValueError):
            r.register(spec)
        for slug in ("bare", "bad/path.x", "a." + "x" * 70):
            with self.subTest(slug=slug), self.assertRaises(ValueError):
                r.register(
                    Endpoint(slug, spec.handler, INPUT, "N", "", "read", "brain:read", (1, 60))
                )

    def test_handler_errors_are_generic_and_budget_checked(self):
        r = Registry()

        def bad(ctx, params):
            raise RuntimeError("private implementation detail")

        r.register(Endpoint("test.bad", bad, INPUT, "Bad", "", "read", "brain:read", (60, 60)))
        ctx = CallContext(
            workspace=object(), principal="user", permissions=frozenset({"brain:read"})
        )
        with (
            self.assertLogs("core.api.registry", level="ERROR"),
            self.assertRaises(ApiError) as err,
        ):
            r.call("test.bad", ctx, {"n": 1})
        self.assertEqual(err.exception.status, 500)
        self.assertNotIn("private", err.exception.message)
        expired = CallContext(
            workspace=object(), principal="user", permissions=ctx.permissions, deadline=0
        )
        with self.assertRaises(ApiError) as err:
            r.call("test.bad", expired, {"n": 1})
        self.assertEqual(err.exception.status, 504)


class RESTTests(TestCase):
    def setUp(self):
        _mock_setup(self)
        self.ws = Workspace.get_default()
        self.other = Workspace.objects.create(name="Other")
        self.raw = APIToken.generate_token(scope="api")
        self.token = APIToken.objects.create(
            name="reader",
            token=self.raw,
            scope="api",
            permissions=["brain:read"],
            workspace=self.ws,
        )
        saved = dict(registry._endpoints)
        self.addCleanup(lambda: setattr(registry, "_endpoints", saved))
        registry._endpoints = {}

        @endpoint("test.echo", title="Echo", input=INPUT, permission="brain:read")
        def echo(ctx, params):
            return {"workspace": str(ctx.workspace.pk), "n": params["n"]}

        @endpoint(
            "test.write", title="Write", input=INPUT, permission="brain:write", access="write"
        )
        def write(ctx, params):
            return params

    def get(self, path="test.echo?n=2", raw=None, **kwargs):
        return self.client.get(
            "/api/v1/" + path,
            HTTP_AUTHORIZATION="Bearer " + (self.raw if raw is None else raw),
            **kwargs,
        )

    def test_post_and_get_call_the_same_handler_with_token_workspace(self):
        post = self.client.post(
            "/api/v1/test.echo", {"n": 2}, content_type="application/json", HTTP_X_API_KEY=self.raw
        )
        self.assertEqual(post.json(), self.get().json())
        self.assertEqual(post.json(), {"ok": True, "data": {"workspace": str(self.ws.pk), "n": 2}})
        self.token.refresh_from_db()
        self.assertIsNotNone(self.token.last_used_at)
        raw = APIToken.generate_token(scope="api")
        APIToken.objects.create(
            name="other", token=raw, scope="api", permissions=["brain:read"], workspace=self.other
        )
        self.assertEqual(self.get(raw=raw).json()["data"]["workspace"], str(self.other.pk))

    def test_stored_digest_is_not_a_token_and_legacy_scopes_do_not_expand(self):
        self.assertEqual(self.token.token, APIToken.digest(self.raw))
        self.assertEqual(self.get(raw=self.token.token).status_code, 401)
        legacy = APIToken.objects.create(
            name="old", token=APIToken.generate_token(), workspace=self.ws
        )
        self.assertEqual(self.get(raw=legacy.raw_token).status_code, 403)
        self.assertEqual(legacy.token, APIToken.digest(legacy.raw_token))  # F53
        self.assertEqual(self.get(raw=legacy.token).status_code, 401)
        for url in ("/api/v1/datastores/", "/api/v1/plugins/"):
            self.assertIn(self.client.get(url, HTTP_X_API_KEY=self.raw).status_code, (401, 403))
            self.assertIn(
                self.client.get(url, HTTP_X_API_KEY=self.token.token).status_code, (401, 403)
            )

    def test_auth_failures_are_constant_shape(self):
        expected = self.get(raw="bogus").json()
        self.token.expires_at = timezone.now() - timedelta(seconds=1)
        self.token.save()
        self.assertEqual(self.get().json(), expected)
        self.token.expires_at = None
        self.token.is_active = False
        self.token.save()
        self.assertEqual(self.get().json(), expected)
        self.assertEqual(self.get(raw="").json(), expected)

    @override_settings(API_AUTHFAIL_RATE_LIMIT=2)
    def test_authfail_limit_is_per_ip_and_precedes_lookup(self):
        self.assertEqual(self.get(raw="bad1").status_code, 401)
        self.assertEqual(self.get(raw="bad2").status_code, 401)
        self.assertEqual(self.get(raw="bad3").status_code, 429)
        with mock.patch("core.api.errors.APIToken.objects.select_related") as query:
            self.assertEqual(self.get().status_code, 429)
        query.assert_not_called()

    def test_catalog_is_permission_filtered_and_no_vectors_endpoints(self):
        slugs = [x["slug"] for x in self.get("_catalog").json()["data"]["endpoints"]]
        self.assertEqual(slugs, ["test.echo"])
        self.assertEqual(self.get("_health").json()["data"], {"status": "ok"})
        self.assertEqual(self.get("test.write?n=1").status_code, 405)
        result = self.client.post(
            "/api/v1/test.write", {"n": 1}, content_type="application/json", HTTP_X_API_KEY=self.raw
        )
        self.assertEqual((result.status_code, result.json()["error"]["code"]), (403, "forbidden"))
        self.assertEqual(self.get("vectors.count").status_code, 404)

    def test_validation_and_cors_on_every_outcome(self):
        for path in (
            "test.echo?n=2&extra=1",
            "test.echo?n=2&n=3",
            "test.echo?n=true",
            "test.echo?n=5",
        ):
            with self.subTest(path=path):
                response = self.get(path)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response["Access-Control-Allow-Origin"], "*")
        response = self.client.generic(
            "GET", "/api/v1/test.echo?n=2", b"{}", HTTP_X_API_KEY=self.raw
        )
        self.assertEqual(response.status_code, 400)
        response = self.client.options("/api/v1/test.echo")
        self.assertEqual(response.status_code, 204)
        self.assertIn("POST", response["Access-Control-Allow-Methods"])

    def test_endpoint_rate_limit_has_retry_after_and_a_fresh_window(self):
        with mock.patch("core.ratelimit.time") as clock:
            clock.time.return_value = 1201
            for _ in range(60):
                self.assertEqual(self.get().status_code, 200)
            response = self.get()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["retry_after"], int(response["Retry-After"]))
        with mock.patch("core.ratelimit.time") as clock:
            clock.time.return_value = 1261
            self.assertEqual(self.get().status_code, 200)

    def test_null_workspace_and_cross_workspace_binding_fail_closed(self):
        APIToken.objects.filter(pk=self.token.pk).update(workspace=None)
        self.assertEqual(self.get().status_code, 403)
        brain = Brain.objects.create(workspace=self.other, name="B", slug="b")
        APIToken.objects.filter(pk=self.token.pk).update(workspace=self.ws, brain=brain)
        self.assertEqual(self.get().status_code, 401)

    def test_url_tokens_cannot_be_used_as_header_tokens_or_have_write_permission(self):
        raw = APIToken.generate_token(scope="api", kind="url")
        token = APIToken(
            name="url",
            token=raw,
            scope="api",
            kind="url",
            workspace=self.ws,
            permissions=["brain:read"],
            expires_at=timezone.now() + timedelta(days=1),
        )
        token.save()
        self.assertEqual(self.get(raw=raw).status_code, 401)
        token.permissions = ["brain:write"]
        with self.assertRaises(ValidationError):
            token.save()
        token.permissions = ["brain:read"]
        token.expires_at = None
        with self.assertRaises(ValidationError):
            token.save()

    def test_db_constraints_enforce_scope_permissions_and_url_expiry(self):
        for changes in ({"permissions": []}, {"kind": "url", "expires_at": None}):
            with (
                self.subTest(changes=changes),
                self.assertRaises(IntegrityError),
                transaction.atomic(),
            ):
                APIToken.objects.filter(pk=self.token.pk).update(**changes)

    def test_console_hashes_new_token_and_reveals_it_once(self):
        user = User.objects.create(email="api@example.test", is_superuser=True)
        self.client.force_login(user)
        response = self.client.post(
            reverse("cpanel:api_token_create"),
            {
                "name": "console",
                "scope": "api",
                "permissions": ["brain:read"],
                "kind": "bearer",
                "brain_tier": "public",
            },
        )
        self.assertEqual(response.status_code, 302)
        token = APIToken.objects.get(name="console")
        raw = self.client.session["new_api_token"]
        self.assertEqual(token.token, APIToken.digest(raw))
        self.assertContains(self.client.get(response.url), raw)
        self.assertEqual(self.client.get(response.url).status_code, 302)
        listing = self.client.get(reverse("cpanel:api_token_list"))
        self.assertNotContains(listing, raw)
        self.assertNotContains(listing, token.token)
        self.assertContains(listing, "brain:read")

    def test_form_cannot_bind_to_another_workspace(self):
        brain = Brain.objects.create(workspace=self.other, name="B", slug="b")
        form = APITokenForm(
            {
                "name": "bad",
                "scope": "api",
                "permissions": ["brain:read"],
                "kind": "bearer",
                "brain_tier": "public",
                "brain": str(brain.pk),
            },
            workspace=self.ws,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("brain", form.errors)
