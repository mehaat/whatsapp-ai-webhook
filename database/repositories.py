"""
database/repositories.py
-------------------------
Repository pattern over the ORM models (v4.0).

Repositories encapsulate all query/persistence logic so services never issue
raw ORM queries. Each repository is constructed with an active SQLAlchemy
``Session``.
"""

from __future__ import annotations

from typing import List, Optional

from sqlalchemy import select

from database.models import AILog, Conversation, Customer, Merchant, OAuthToken, Settings


class BaseRepository:
    """Common base holding the active session."""

    def __init__(self, session) -> None:
        self.session = session


class MerchantRepository(BaseRepository):
    def upsert(self, shop_domain: str, name: Optional[str] = None) -> Merchant:
        merchant = self.session.scalar(
            select(Merchant).where(Merchant.shop_domain == shop_domain)
        )
        if merchant is None:
            merchant = Merchant(shop_domain=shop_domain, name=name, installed=True)
            self.session.add(merchant)
        else:
            merchant.installed = True
            if name:
                merchant.name = name
        return merchant

    def get(self, shop_domain: str) -> Optional[Merchant]:
        return self.session.scalar(
            select(Merchant).where(Merchant.shop_domain == shop_domain)
        )


class OAuthTokenRepository(BaseRepository):
    def set_token(self, shop_domain: str, access_token: str, scopes: str = "") -> OAuthToken:
        row = self.session.scalar(
            select(OAuthToken).where(OAuthToken.shop_domain == shop_domain)
        )
        if row is None:
            row = OAuthToken(shop_domain=shop_domain, access_token=access_token, scopes=scopes)
            self.session.add(row)
        else:
            row.access_token = access_token
            row.scopes = scopes
        return row

    def get_token(self, shop_domain: str) -> Optional[str]:
        row = self.session.scalar(
            select(OAuthToken).where(OAuthToken.shop_domain == shop_domain)
        )
        return row.access_token if row else None


class CustomerRepository(BaseRepository):
    def upsert(
        self, wa_number: str, profile_name: Optional[str] = None, language: Optional[str] = None
    ) -> Customer:
        customer = self.session.scalar(
            select(Customer).where(Customer.wa_number == wa_number)
        )
        if customer is None:
            customer = Customer(
                wa_number=wa_number, profile_name=profile_name, language=language
            )
            self.session.add(customer)
        else:
            if profile_name:
                customer.profile_name = profile_name
            if language:
                customer.language = language
        return customer


class ConversationRepository(BaseRepository):
    def add_turn(self, wa_number: str, role: str, text: str) -> Conversation:
        turn = Conversation(wa_number=wa_number, role=role, text=text)
        self.session.add(turn)
        return turn

    def recent(self, wa_number: str, limit: int = 10) -> List[Conversation]:
        return list(
            self.session.scalars(
                select(Conversation)
                .where(Conversation.wa_number == wa_number)
                .order_by(Conversation.id.desc())
                .limit(limit)
            )
        )


class AILogRepository(BaseRepository):
    def create(
        self,
        wa_number: str,
        user_message: str,
        reply: str,
        context: Optional[str] = None,
        model: Optional[str] = None,
    ) -> AILog:
        row = AILog(
            wa_number=wa_number,
            user_message=user_message,
            reply=reply,
            context=context,
            model=model,
        )
        self.session.add(row)
        return row


class SettingsRepository(BaseRepository):
    def get(self, key: str, shop_domain: Optional[str] = None) -> Optional[str]:
        row = self.session.scalar(
            select(Settings).where(
                Settings.key == key, Settings.shop_domain == shop_domain
            )
        )
        return row.value if row else None

    def set(self, key: str, value: str, shop_domain: Optional[str] = None) -> Settings:
        row = self.session.scalar(
            select(Settings).where(
                Settings.key == key, Settings.shop_domain == shop_domain
            )
        )
        if row is None:
            row = Settings(key=key, value=value, shop_domain=shop_domain)
            self.session.add(row)
        else:
            row.value = value
        return row
