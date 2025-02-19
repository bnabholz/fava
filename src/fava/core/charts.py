"""Provide data suitable for Fava's charts."""
from __future__ import annotations

from dataclasses import dataclass
from dataclasses import fields
from dataclasses import is_dataclass
from datetime import date
from datetime import timedelta
from typing import Any
from typing import Generator
from typing import Pattern
from typing import TYPE_CHECKING

from beancount.core import realization
from beancount.core.amount import Amount
from beancount.core.data import Booking
from beancount.core.data import iter_entry_dates
from beancount.core.data import Transaction
from beancount.core.display_context import DisplayContext
from beancount.core.inventory import Inventory
from beancount.core.number import Decimal
from beancount.core.number import MISSING
from beancount.core.position import Position
from simplejson import JSONEncoder
from simplejson import loads

from fava.core._compat import FLAG_UNREALIZED
from fava.core.conversion import cost_or_value
from fava.core.conversion import units
from fava.core.module_base import FavaModule
from fava.core.tree import SerialisedTreeNode
from fava.core.tree import Tree
from fava.helpers import FavaAPIException
from fava.util import listify
from fava.util import pairwise
from fava.util.date import Interval

try:
    from flask.json.provider import JSONProvider
except ImportError:
    pass

if TYPE_CHECKING:  # pragma: no cover
    from flask import Flask

    from fava.core import FilteredLedger


ONE_DAY = timedelta(days=1)


def inv_to_dict(inventory: Inventory) -> dict[str, Decimal]:
    """Convert an inventory to a simple cost->number dict."""
    return {
        pos.units.currency: pos.units.number
        for pos in inventory
        if pos.units.number is not None
    }


Inventory.for_json = inv_to_dict  # type: ignore


class FavaJSONEncoder(JSONEncoder):
    """Allow encoding some Beancount date structures."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Allow use of a `for_json` method to serialise dict subclasses.
        kwargs["for_json"] = True
        # Sort dict keys (Flask also does this by default).
        kwargs["sort_keys"] = True
        super().__init__(*args, **kwargs)

    def default(self, o: Any) -> Any:
        # pylint: disable=too-many-return-statements
        if isinstance(o, (date, Amount, Booking, DisplayContext, Position)):
            return str(o)
        if isinstance(o, (set, frozenset)):
            return list(o)
        if isinstance(o, Pattern):
            return o.pattern
        if is_dataclass(o):
            return {field.name: getattr(o, field.name) for field in fields(o)}
        if o is MISSING:
            return None
        return JSONEncoder.default(self, o)


ENCODER = FavaJSONEncoder()
PRETTY_ENCODER = FavaJSONEncoder(indent=True)


def setup_json_for_app(app: Flask) -> None:
    """Use custom JSON encoder."""
    if hasattr(app, "json_provider_class"):  # Flask >=2.2

        class FavaJSONProvider(JSONProvider):
            """Use custom JSON encoder and decoder."""

            def dumps(
                self, obj: Any, *, _option: Any = None, **_kwargs: Any
            ) -> Any:
                return ENCODER.encode(obj)

            def loads(self, s: str | bytes, **_kwargs: Any) -> Any:
                return loads(s)

        app.json = FavaJSONProvider(app)

    else:  # pragma: no cover
        app.json_encoder = FavaJSONEncoder  # type: ignore


@dataclass
class DateAndBalance:
    """Balance at a date."""

    date: date
    balance: dict[str, Decimal] | Inventory


@dataclass
class DateAndBalanceWithBudget:
    """Balance at a date with a budget."""

    date: date
    balance: Inventory
    account_balances: dict[str, Inventory]
    budgets: dict[str, Decimal]


class ChartModule(FavaModule):
    """Return data for the various charts in Fava."""

    def hierarchy(
        self,
        filtered: FilteredLedger,
        account_name: str,
        conversion: str,
        begin: date | None = None,
        end: date | None = None,
    ) -> SerialisedTreeNode:
        """Render an account tree."""
        if begin is not None and end is not None:
            tree = Tree(iter_entry_dates(filtered.entries, begin, end))
        else:
            tree = filtered.root_tree
        return tree.get(account_name).serialise(
            conversion, self.ledger.price_map, end - ONE_DAY if end else None
        )

    @listify
    def interval_totals(
        self,
        filtered: FilteredLedger,
        interval: Interval,
        accounts: str | tuple[str],
        conversion: str,
        invert: bool = False,
    ) -> Generator[DateAndBalanceWithBudget, None, None]:
        """Render totals for account (or accounts) in the intervals.

        Args:
            interval: An interval.
            accounts: A single account (str) or a tuple of accounts.
            conversion: The conversion to use.
            invert: invert all numbers.
        """
        # pylint: disable=too-many-locals
        price_map = self.ledger.price_map
        for begin, end in pairwise(filtered.interval_ends(interval)):
            inventory = Inventory()
            entries = iter_entry_dates(filtered.entries, begin, end)
            account_inventories = {}
            for entry in (e for e in entries if isinstance(e, Transaction)):
                for posting in entry.postings:
                    if posting.account.startswith(accounts):
                        if posting.account not in account_inventories:
                            account_inventories[posting.account] = Inventory()
                        account_inventories[posting.account].add_position(
                            posting
                        )
                        inventory.add_position(posting)
            balance = cost_or_value(
                inventory, conversion, price_map, end - ONE_DAY
            )
            account_balances = {}
            for account, acct_value in account_inventories.items():
                account_balances[account] = cost_or_value(
                    acct_value,
                    conversion,
                    price_map,
                    end - ONE_DAY,
                )
            budgets = {}
            if isinstance(accounts, str):
                budgets = self.ledger.budgets.calculate_children(
                    accounts, begin, end
                )

            if invert:
                # pylint: disable=invalid-unary-operand-type
                balance = -balance
                budgets = {k: -v for k, v in budgets.items()}
                account_balances = {k: -v for k, v in account_balances.items()}

            yield DateAndBalanceWithBudget(
                begin,
                balance,
                account_balances,
                budgets,
            )

    @listify
    def linechart(
        self, filtered: FilteredLedger, account_name: str, conversion: str
    ) -> Generator[DateAndBalance, None, None]:
        """Get the balance of an account as a line chart.

        Args:
            account_name: A string.
            conversion: The conversion to use.

        Returns:
            A list of dicts for all dates on which the balance of the given
            account has changed containing the balance (in units) of the
            account at that date.
        """
        real_account = realization.get_or_create(
            filtered.root_account, account_name
        )
        postings = realization.get_postings(real_account)
        journal = realization.iterate_with_balance(postings)

        # When the balance for a commodity just went to zero, it will be
        # missing from the 'balance' so keep track of currencies that last had
        # a balance.
        last_currencies = None

        price_map = self.ledger.price_map
        for entry, _, change, balance_inventory in journal:
            if change.is_empty():
                continue

            balance = inv_to_dict(
                cost_or_value(
                    balance_inventory, conversion, price_map, entry.date
                )
            )

            currencies = set(balance.keys())
            if last_currencies:
                for currency in last_currencies - currencies:
                    balance[currency] = 0
            last_currencies = currencies

            yield DateAndBalance(entry.date, balance)

    @listify
    def net_worth(
        self, filtered: FilteredLedger, interval: Interval, conversion: str
    ) -> Generator[DateAndBalance, None, None]:
        """Compute net worth.

        Args:
            interval: A string for the interval.
            conversion: The conversion to use.

        Returns:
            A list of dicts for all ends of the given interval containing the
            net worth (Assets + Liabilities) separately converted to all
            operating currencies.
        """
        transactions = (
            entry
            for entry in filtered.entries
            if (
                isinstance(entry, Transaction)
                and entry.flag != FLAG_UNREALIZED
            )
        )

        types = (
            self.ledger.options["name_assets"],
            self.ledger.options["name_liabilities"],
        )

        txn = next(transactions, None)
        inventory = Inventory()

        price_map = self.ledger.price_map
        for end_date in filtered.interval_ends(interval):
            while txn and txn.date < end_date:
                for posting in txn.postings:
                    if posting.account.startswith(types):
                        inventory.add_position(posting)
                txn = next(transactions, None)
            yield DateAndBalance(
                end_date,
                cost_or_value(
                    inventory, conversion, price_map, end_date - ONE_DAY
                ),
            )

    @staticmethod
    def can_plot_query(types: list[tuple[str, Any]]) -> bool:
        """Whether we can plot the given query.

        Args:
            types: The list of types returned by the BQL query.
        """
        return (
            len(types) == 2
            and types[0][1] in {str, date}
            and types[1][1] is Inventory
        )

    def query(
        self, types: list[tuple[str, Any]], rows: list[tuple[Any, ...]]
    ) -> Any:
        """Chart for a query.

        Args:
            types: The list of result row types.
            rows: The result rows.
        """
        if not self.can_plot_query(types):
            raise FavaAPIException("Can not plot the given chart.")
        if types[0][1] is date:
            return [
                {"date": date, "balance": units(inv)} for date, inv in rows
            ]
        return [{"group": group, "balance": units(inv)} for group, inv in rows]
