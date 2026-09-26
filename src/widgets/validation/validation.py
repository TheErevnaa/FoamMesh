#!/usr/bin/env python
# -*- coding: utf-8 -*-

from PySide6.QtCore import QObject

from foammesh.support import field_complaint


def isEditable(edit):
    return edit.isVisible() and edit.isEnabled()


class Validator(QObject):
    def __init__(self):
        super().__init__()

    def validate(self):
        raise NotImplementedError


class FormValidator(Validator):
    def __init__(self):
        super().__init__()

        self._required = []
        self._validations = []

    def addRequiredValidation(self, edit, name):
        self._required.append((edit, name))

    def addCustomValidation(self, validator):
        self._validations.append(validator)

    def validate(self):
        for edit, name in self._required:
            if isEditable(edit) and not edit.text().strip():
                return False, field_complaint.sentence(
                    name, field_complaint.required_clause())

        for v in self._validations:
            valid, msg = v.validate()
            if not valid:
                return False, msg

        return True, None


class FloatValidator(Validator):
    def __init__(self, edit, name):
        super().__init__()

        self._edit = edit
        self._name = name

        self._lowLimit = None
        self._lowLimitInclusive = True
        self._highLimit = None
        self._highLimitInclusive = True

    def setLowLimit(self, limit, inclusive=True):
        self._lowLimit = limit
        self._lowLimitInclusive = inclusive

        return self

    def setHighLimit(self, limit, inclusive=True):
        self._highLimit = limit
        self._highLimitInclusive = inclusive

        return self

    def setRange(self, low, high):
        self._lowLimit = low
        self._highLimit = high
        self._lowLimitInclusive = True
        self._highLimitInclusive = True

        return self

    def validate(self):
        if not self._edit.text().strip():
            return False, field_complaint.sentence(
                self._name, field_complaint.required_clause())

        try:
            value = float(self._edit.text())
        except ValueError:
            return False, field_complaint.sentence(
                self._name, field_complaint.number_clause())

        # One clause, naming the field and spelling out the whole permitted
        # span. The old text was an unnamed 'Out of Range: ' followed by that
        # span in mathematical notation, so a reader could not tell which box
        # it meant.
        low, high = self._lowLimit, self._highLimit
        if ((low is not None
                and (value < low or (value == low and not self._lowLimitInclusive)))
                or (high is not None
                    and (value > high or (value == high and not self._highLimitInclusive)))):
            return False, field_complaint.sentence(self._name, field_complaint.range_clause(
                low=low, high=high,
                lowInclusive=self._lowLimitInclusive,
                highInclusive=self._highLimitInclusive))

        return True, value


class NotGreaterValidator(FloatValidator):
    def __init__(self, myEdit, otherEdit, myName, otherName):
        super().__init__(myEdit, myName)

        self._other = otherEdit
        self._otherName = otherName

    def validate(self):
        valid, me = super().validate()
        if not valid:
            return valid, me

        valid, other = FloatValidator(self._other, self._otherName).validate()
        if not valid:
            return valid, other

        if me > other:
            return False, field_complaint.sentence(
                self._name,
                field_complaint.no_greater_than_clause(self._otherName))

        return True, None
