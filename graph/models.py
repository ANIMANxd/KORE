"""
Knowledge graph models for KORE.

Represents entities (rules, teams, processes, etc.) and the
relationships between them extracted from business rules.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


EntityType = Literal[
    "rule",
    "team",
    "process",
    "system",
    "policy",
    "person_role",
    "threshold",
    "concept",
]

RelationshipType = Literal[
    "governs",
    "involves",
    "depends_on",
    "contradicts",
    "supersedes",
    "applies_to",
    "triggers",
    "owned_by",
]


class Entity(BaseModel):
    """A node in the knowledge graph."""

    entity_id: str = Field(default_factory=lambda: str(uuid4()))
    entity_type: EntityType
    name: str = Field(min_length=1)
    description: str | None = None
    source_rule_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    mention_count: int = Field(default=1, ge=0)

    def bump_mention(self) -> None:
        """Increment mention count and update last_seen timestamp."""
        self.mention_count += 1
        self.last_seen = datetime.now(timezone.utc)


class Relationship(BaseModel):
    """An edge between two entities in the knowledge graph."""

    relationship_id: str = Field(default_factory=lambda: str(uuid4()))
    from_entity_id: str = Field(min_length=1)
    to_entity_id: str = Field(min_length=1)
    relationship_type: RelationshipType
    confidence: float = Field(ge=0.0, le=1.0)
    source_rule_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class KnowledgeGraph(BaseModel):
    """Container for all entities and relationships extracted from rules."""

    entities: dict[str, Entity] = Field(default_factory=dict)
    relationships: list[Relationship] = Field(default_factory=list)
    last_updated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    total_rules_processed: int = Field(default=0, ge=0)

    def to_json(self) -> str:
        """Serialize the knowledge graph to a JSON string."""
        return json.dumps(
            self.model_dump(mode="json"),
            indent=2,
            ensure_ascii=False,
        )

    def summary(self) -> str:
        """Return a human-readable stats summary of the graph."""
        entity_type_counts: dict[str, int] = {}
        for entity in self.entities.values():
            entity_type_counts[entity.entity_type] = (
                entity_type_counts.get(entity.entity_type, 0) + 1
            )

        rel_type_counts: dict[str, int] = {}
        for rel in self.relationships:
            rel_type_counts[rel.relationship_type] = (
                rel_type_counts.get(rel.relationship_type, 0) + 1
            )

        lines = [
            "Knowledge Graph Summary",
            "=" * 40,
            f"Total rules processed : {self.total_rules_processed}",
            f"Total entities        : {len(self.entities)}",
            f"Total relationships   : {len(self.relationships)}",
            f"Last updated          : {self.last_updated.isoformat()}",
            "",
            "Entity breakdown",
            "-" * 20,
        ]
        for etype, count in sorted(entity_type_counts.items()):
            lines.append(f"  {etype:<15} : {count}")

        lines.extend([
            "",
            "Relationship breakdown",
            "-" * 20,
        ])
        for rtype, count in sorted(rel_type_counts.items()):
            lines.append(f"  {rtype:<15} : {count}")

        return "\n".join(lines)

    def add_entity(self, entity: Entity) -> str:
        """Add an entity to the graph. Returns the entity_id."""
        self.entities[entity.entity_id] = entity
        self.last_updated = datetime.now(timezone.utc)
        return entity.entity_id

    def add_relationship(self, relationship: Relationship) -> None:
        """Add a relationship to the graph."""
        self.relationships.append(relationship)
        self.last_updated = datetime.now(timezone.utc)

    def merge_entity(
        self,
        entity_type: EntityType,
        name: str,
        description: str | None = None,
        source_rule_id: str | None = None,
    ) -> Entity:
        """
        Upsert an entity by (entity_type, name).

        If an entity with the same type and name already exists, bump its
        mention count and append the source rule id. Otherwise create a new
        entity. Returns the entity (existing or new).
        """
        key = f"{entity_type}:{name.lower().strip()}"
        for entity in self.entities.values():
            if f"{entity.entity_type}:{entity.name.lower().strip()}" == key:
                entity.bump_mention()
                if source_rule_id and source_rule_id not in entity.source_rule_ids:
                    entity.source_rule_ids.append(source_rule_id)
                if description and not entity.description:
                    entity.description = description
                self.last_updated = datetime.now(timezone.utc)
                return entity

        new_entity = Entity(
            entity_type=entity_type,
            name=name.strip(),
            description=description,
            source_rule_ids=[source_rule_id] if source_rule_id else [],
        )
        self.add_entity(new_entity)
        return new_entity

    def get_entity(self, entity_id: str) -> Entity | None:
        """Retrieve an entity by its id."""
        return self.entities.get(entity_id)

    def get_relationships_from(self, entity_id: str) -> list[Relationship]:
        """Return all relationships where entity_id is the source."""
        return [r for r in self.relationships if r.from_entity_id == entity_id]

    def get_relationships_to(self, entity_id: str) -> list[Relationship]:
        """Return all relationships where entity_id is the target."""
        return [r for r in self.relationships if r.to_entity_id == entity_id]

    def get_related_entities(self, entity_id: str) -> list[Entity]:
        """Return all entities directly connected to the given entity."""
        related_ids: set[str] = set()
        for rel in self.relationships:
            if rel.from_entity_id == entity_id:
                related_ids.add(rel.to_entity_id)
            elif rel.to_entity_id == entity_id:
                related_ids.add(rel.from_entity_id)
        return [self.entities[eid] for eid in related_ids if eid in self.entities]
