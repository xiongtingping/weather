"""Small deterministic linear-algebra helpers for the five-state kernel."""

from __future__ import annotations

import math
from typing import Iterable, Sequence


TOL = 1e-10


def identity(size: int) -> list[list[float]]:
    return [[1.0 if row == column else 0.0 for column in range(size)] for row in range(size)]


def transpose(matrix: Sequence[Sequence[float]]) -> list[list[float]]:
    return [list(row) for row in zip(*matrix)]


def matmul(left: Sequence[Sequence[float]], right: Sequence[Sequence[float]]) -> list[list[float]]:
    right_t = transpose(right)
    return [[sum(float(a) * float(b) for a, b in zip(row, column)) for column in right_t] for row in left]


def matvec(matrix: Sequence[Sequence[float]], vector: Sequence[float]) -> list[float]:
    return [sum(float(a) * float(b) for a, b in zip(row, vector)) for row in matrix]


def add(left: Sequence[Sequence[float]], right: Sequence[Sequence[float]]) -> list[list[float]]:
    return [[float(a) + float(b) for a, b in zip(row_a, row_b)] for row_a, row_b in zip(left, right)]


def symmetrize(matrix: Sequence[Sequence[float]]) -> list[list[float]]:
    size = len(matrix)
    return [[0.5 * (float(matrix[i][j]) + float(matrix[j][i])) for j in range(size)] for i in range(size)]


def cholesky_psd(matrix: Sequence[Sequence[float]], tolerance: float = TOL) -> list[list[float]]:
    """Return a lower factor, allowing zero eigenvalues within tolerance."""

    size = len(matrix)
    if any(len(row) != size for row in matrix):
        raise ValueError("covariance_not_square")
    for i in range(size):
        for j in range(size):
            if not math.isfinite(float(matrix[i][j])):
                raise ValueError("covariance_non_finite")
            if abs(float(matrix[i][j]) - float(matrix[j][i])) > tolerance:
                raise ValueError("covariance_not_symmetric")
    lower = [[0.0 for _ in range(size)] for _ in range(size)]
    for i in range(size):
        for j in range(i + 1):
            residual = float(matrix[i][j]) - sum(lower[i][k] * lower[j][k] for k in range(j))
            if i == j:
                if residual < -tolerance:
                    raise ValueError("covariance_not_positive_semidefinite")
                lower[i][j] = math.sqrt(max(0.0, residual))
            elif lower[j][j] > tolerance:
                lower[i][j] = residual / lower[j][j]
            elif abs(residual) > tolerance:
                raise ValueError("covariance_not_positive_semidefinite")
    return lower


def inverse(matrix: Sequence[Sequence[float]]) -> list[list[float]]:
    size = len(matrix)
    if size == 0 or any(len(row) != size for row in matrix):
        raise ValueError("matrix_inverse_dimension_invalid")
    augmented = [list(map(float, row)) + identity(size)[i] for i, row in enumerate(matrix)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) <= TOL:
            raise ValueError("matrix_singular")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        scale = augmented[column][column]
        augmented[column] = [value / scale for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                value - factor * pivot_value
                for value, pivot_value in zip(augmented[row], augmented[column])
            ]
    return [row[size:] for row in augmented]


def determinant(matrix: Sequence[Sequence[float]]) -> float:
    size = len(matrix)
    working = [list(map(float, row)) for row in matrix]
    result = 1.0
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(working[row][column]))
        if abs(working[pivot][column]) <= TOL:
            return 0.0
        if pivot != column:
            working[column], working[pivot] = working[pivot], working[column]
            result *= -1.0
        pivot_value = working[column][column]
        result *= pivot_value
        for row in range(column + 1, size):
            factor = working[row][column] / pivot_value
            for item in range(column + 1, size):
                working[row][item] -= factor * working[column][item]
    return result


def outer(left: Iterable[float], right: Iterable[float]) -> list[list[float]]:
    left_values = list(left)
    right_values = list(right)
    return [[a * b for b in right_values] for a in left_values]


__all__ = [
    "TOL",
    "add",
    "cholesky_psd",
    "determinant",
    "identity",
    "inverse",
    "matmul",
    "matvec",
    "outer",
    "symmetrize",
    "transpose",
]
