"""Shared, validated zero-based table grid; spans preserve merged cells."""
from typing import Annotated

from pydantic import BaseModel, Field, model_validator

Positive = Annotated[int, Field(ge=1)]


class Cell(BaseModel):
    row: Annotated[int, Field(ge=0)]
    col: Annotated[int, Field(ge=0)]
    rowspan: Positive
    colspan: Positive
    header: bool
    text: str


class TableGrid(BaseModel):
    rows: Positive
    cols: Positive
    cells: list[Cell]

    @model_validator(mode="after")
    def validate_cells(self):
        if self.rows * self.cols > 100_000:
            raise ValueError("table exceeds viewer limit")
        used = set()
        for cell in self.cells:
            if cell.row + cell.rowspan > self.rows or cell.col + cell.colspan > self.cols:
                raise ValueError("table span out of bounds")
            for row in range(cell.row, cell.row + cell.rowspan):
                for col in range(cell.col, cell.col + cell.colspan):
                    if (row, col) in used:
                        raise ValueError("overlapping table spans")
                    used.add((row, col))
        if len(used) != self.rows * self.cols:
            raise ValueError("incomplete table grid")
        return self
