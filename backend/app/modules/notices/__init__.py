"""Owner notices (11 "Owner notice", 12): emails to a workspace's owners and admins.

Recording (`record`) happens in the transaction that changed the state; site suspensions are recorded by
the `publications_notice_suspension` trigger (0034). Delivery (`delivery.deliver_due`) runs in the
worker's maintenance loop and sends with no transaction open.
"""
