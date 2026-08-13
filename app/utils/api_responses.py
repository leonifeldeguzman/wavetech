"""
Small shared helpers for consistent JSON API responses.

The rest of the repository (auth/dashboard/manifests blueprints) is
server-rendered HTML with no established JSON response convention, so
this introduces one simple, consistent structure for the new Passenger
Portal API endpoints only. It does not touch any existing route.
"""
from flask import jsonify


def success_response(data=None, message=None, status=200):
    body = {"success": True}
    if message is not None:
        body["message"] = message
    body["data"] = data if data is not None else {}
    return jsonify(body), status


def error_response(code, message, status=400, fields=None):
    error = {"code": code, "message": message}
    if fields:
        error["fields"] = fields
    return jsonify({"success": False, "error": error}), status