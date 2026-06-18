from __future__ import annotations

from app.models import System


class SystemRepository:
    @staticmethod
    def find_by_ids(sys_ids: list[int]) -> list[System]:
        return System.query.filter(System.id.in_(sys_ids)).all()

    @staticmethod
    def get_by_id(system_id: int) -> System | None:
        return System.query.get(system_id)

    @staticmethod
    def get_by_id_or_404(system_id: int) -> System:
        return System.query.get_or_404(system_id)

    @staticmethod
    def find_all() -> list[System]:
        return System.query.all()

    @staticmethod
    def find_by_identifier(identifier: str) -> list[System]:
        return System.query.filter(System.identifier == identifier).all()

    @staticmethod
    def find_by_primary_name(name: str) -> list[System]:
        return System.query.filter(System.primary_system_name == name).all()
