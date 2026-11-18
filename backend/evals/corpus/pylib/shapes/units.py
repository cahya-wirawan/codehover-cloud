"""Unit helpers with Unicode identifiers."""

ÄQUATOR_KM = 40075


def größe_in_metern(kilometer):
    """Convert kilometres to metres."""
    return kilometer * 1000


def función_cuadrada(x):
    return x * x


class Längeneinheit:
    """A length unit with its size in metres."""

    def umrechnen(self, wert, ziel):
        return wert * self.meter / ziel.meter
