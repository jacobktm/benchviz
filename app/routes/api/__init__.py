"""API blueprint — split by endpoint group for maintainability."""

from flask import Blueprint

bp = Blueprint('api', __name__)

from . import compare, benchmarks, insights
