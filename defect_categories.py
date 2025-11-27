from enum import Enum


class DefectOrigin(str, Enum):
    """
    Тип образования дефекта.

    Значения храним в машинно‑читаемом виде (латиница),
    а человекочитаемые подписи — в словаре ORIGIN_TITLES.
    """

    SUPPLIER = "supplier"   # Прием товара от поставщика
    CUSTOMER = "customer"   # Прием от покупателя
    WAREHOUSE = "warehouse"  # Дефект обнаружился на складе


# Человекочитаемые названия для показа пользователю
ORIGIN_TITLES = {
    DefectOrigin.SUPPLIER: "Прием товара от поставщика",
    DefectOrigin.CUSTOMER: "Прием от покупателя",
    DefectOrigin.WAREHOUSE: "Дефект обнаружился на складе",
}


# Обратная мапа: текст кнопки -> значение перечисления
TITLE_TO_ORIGIN = {title: origin for origin, title in ORIGIN_TITLES.items()}


def get_origin_titles() -> list[str]:
    """
    Удобный хелпер для получения списка заголовков
    (например, для формирования клавиатуры).
    """

    return list(ORIGIN_TITLES.values())


