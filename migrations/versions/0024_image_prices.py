"""US-24: image sizes and qualities as catalog data.

`model_catalog.capabilities.image_prices` maps each "WxH/quality" a model offers to its price
per image (micro-USD). A model without the map charges `price_image` for every image. The mock
image model gets a new catalog row with its table.

Revision ID: 0024
Revises: 0023
"""

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    INSERT INTO model_catalog (provider, model, kind, price_input, price_output, price_image,
      context_window, max_output_tokens, capabilities, source, effective_from)
    VALUES ('mock', 'mock-image', 'image', 0, 0, 40000, 0, 0,
      '{"image_prices": {"1024x1024/standard": 40000, "1024x1024/hd": 80000,
                         "1024x1536/standard": 60000, "1536x1024/standard": 60000}}',
      'seed:mock', now());
    """)


def downgrade() -> None:
    op.execute("""
    DELETE FROM model_catalog
    WHERE provider = 'mock' AND model = 'mock-image' AND capabilities ? 'image_prices';
    """)
