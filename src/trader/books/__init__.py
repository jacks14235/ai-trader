"""Simulated strategy books.

A book is a portfolio a strategy variant runs on its own: its own cash, its own positions, its own
equity curve. Variants exist so a proposed strategy change can earn its place over a real track
record instead of being adopted on one week of argument.

Nothing in this package may reach a broker. Books consume read-only market data through
`MarketDataSource` and settle through the deterministic simulator in `books.simulator`; the
executor remains the only component that can submit an order anywhere.

Import leaf modules directly; this package deliberately re-exports nothing.
"""
