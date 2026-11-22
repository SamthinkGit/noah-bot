from __future__ import annotations

import random
import json
from dataclasses import dataclass, field
from typing import Literal
from pydantic import BaseModel, Field
from langchain_xai import ChatXAI


StoryMode = Literal["daily_external", "daily_internal", "interaction"]
AI_MODEL = "grok-4.3" 


@dataclass(slots=True)
class CharacterProfile:
    user_id: str
    name: str
    traits: dict[str, str]
    states: dict[str, int]


@dataclass(slots=True)
class ReactionOption:
    emoji: str
    label: str
    description: str
    relation_deltas: dict[str, int] = field(default_factory=dict)
    actor_state_deltas: dict[str, int] = field(default_factory=dict)
    target_state_deltas: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class StoryRequest:
    mode: StoryMode
    guild_name: str
    server_topics: list[str]
    actor: CharacterProfile
    target: CharacterProfile | None = None
    state_definitions: dict[str, str] = field(default_factory=dict)
    relation_definitions: dict[str, dict[str, str]] = field(default_factory=dict)
    delta_limits: dict[str, int] = field(default_factory=dict)
    relation_scores: dict[str, int] = field(default_factory=dict)
    recent_history: list[str] = field(default_factory=list)


@dataclass(slots=True)
class StoryResponse:
    title: str
    scene_description: str
    result_text: str
    relation_impact_text: str
    relation_deltas: dict[str, int] = field(default_factory=dict)
    actor_state_deltas: dict[str, int] = field(default_factory=dict)
    target_state_deltas: dict[str, int] = field(default_factory=dict)
    last_interaction_summary: str = ""
    options: list[ReactionOption] = field(default_factory=list)

class StoryAIModelResponse(BaseModel):
    title: str = Field(description="Un titulo breve para la historia, ideal para un embed de Discord.")
    scene_description: str = Field(description="Una descripcion muy corta de la escena")
    relation_deltas_json: str = Field(description="""
Un json serializable con los datos de los cambios en la relacion, con esta forma:
relations_deltas_json=```json
{
    "friendship": 1,
    "trust": -2,
    "fun": 1,
}
Maximo delta: 2
Minimo delta: -2
```
""")
    actor_state_deltas_json: str = Field(description="""
Un json serializable con los cambios en los estados del actor, con esta forma: 
actor_state_deltas_json=```json
{
    "energy": -1,
    "happiness": 2,
    "confidence": 1,
}
Maximo delta: 2
Minimo delta: -2
```
Siempre usa un salto de linea al principio y al final para asegurar que el formato se mantenga bien al ser generado por el modelo.
"""
    )
    target_state_deltas_json: str = Field(description="""
Un json serializable con los cambios en los estados del target, con esta forma:
target_state_deltas_json=```json
{
    "energy": -2,
    "happiness": 1,
    "social_battery": -1,
}
Maximo delta: 2
Minimo delta: -2
```
Si el target es null, este campo debe ser un json vacio `{}`.
""")
    result_text: str = Field(description="Un texto muy corto que describe el resultado de la escena de forma corta, ideal para un embed de Discord.")
    summary: str = Field(description="Un resumen de una linea que capture lo esencial de la historia")

class StoryAIWithOptionsModelResponse(BaseModel):
    title: str = Field(description="Un titulo breve para la historia, ideal para un embed de Discord.")
    scene_description: str = Field(description="Una descripcion muy corta de la escena")
    options: str = Field(description="""
Un json serializable con las opciones de reaccion, con esta forma:
```json
{
    "options": [
        {
            "emoji": "⚔️",
            "label": "Competir un poco",
            "description": "Convertir el momento en un duelo amistoso.",
            "relation_deltas": {
                "support": -2,
                "admiration": 2,
                "fun": 2
            },
            "actor_state_deltas": {
                "energy": -2
            },
            "target_state_deltas": {
                "energy": 2
            }
        },
        {
            "emoji": "🤝",
            ...
        },
    ]
}
Maximo delta: 2
Minimo delta: -2
```
""")
    summary: str = Field(description="Un resumen de una linea que capture lo esencial de la historia")
class NoahGochiStoryService:
    
    """Mock service for Noah Gochi story generation.

    This class is the only place you need to replace when you want real AI.
    The command layer already knows how to:
    - build a `StoryRequest`
    - call this service
    - apply the returned deltas to characters and relations
    - render the story in Discord embeds

    So your real implementation only needs to transform a `StoryRequest`
    into a valid `StoryResponse`.

    What you should implement later
    --------------------------------
    Replace the body of:
    - `generate_daily_story(...)`
    - `generate_interaction_story(...)`

    You can keep the helper mocks, remove them, or use them as fallback.

    Contract you must preserve
    --------------------------
    1. Both public methods must always return `StoryResponse`.
    2. Do not return raw dicts or model JSON directly; parse and normalize first.
    3. `relation_deltas` keys must match the relation axis keys defined in
       `noah_gochi.py`:
       - `quirkiness`
       - `romance`
       - `loyalty`
       - `friendship`
       - `dominance`
       - `trust`
       - `fun`
       - `admiration`
       - `stability`
       - `support`
    4. Positive relation values always push toward the LEFT side of the axis.
       Examples:
       - `friendship: +4` => more amistad
       - `friendship: -4` => more indiferencia
       - `trust: +3` => more confianza
       - `trust: -3` => more sospecha
    5. `actor_state_deltas` and `target_state_deltas` keys should only use the
       keys exposed in `request.state_definitions`.
    6. State deltas should stay reasonably small so stories feel gradual.
       Recommended range: `[-15, 15]`
    7. Relation deltas should also stay compact.
       Recommended range: `[-8, 8]`
    8. `last_interaction_summary` should be short and storable, because it is used
       later in `.noah gochi relation` as the latest remembered interaction.
    9. For `interaction`, return 2 or 3 options in `options`, and precompute the
       consequences of each option there.

    Data available inside `StoryRequest`
    ------------------------------------
    `request.state_definitions`
    Mapping of valid character-state keys to their meaning. Example:
    - `energy`: Physical or mental energy available after the event.
    - `happiness`: Emotional wellbeing or mood after the event.
    - `confidence`: How secure the character feels about themselves.
    - `social_battery`: How much social capacity the character has left.

    `request.relation_definitions`
    Mapping of valid relation keys to their axis semantics. Example:
    - `friendship["left"]` => `Amistad`
    - `friendship["right"]` => `Indiferencia`
    - `friendship["positive_meaning"]` => increasing friendship
    - `friendship["negative_meaning"]` => increasing indifference

    This is the metadata you will normally want to inject into your AI prompt so
    the model knows which deltas are legal and what each sign means.
    """

    def __init__(self, rng: random.Random | None = None) -> None:
        self.rng = rng or random.Random()

    def generate_daily_story(self, request: StoryRequest) -> StoryResponse:
        """Generate a full resolved daily story.

        This powers the command `.noah gochi daily`.

        Input:
        - `request.actor`: the user running the daily
        - `request.target`: another Noah Gochi for shared dailies, or `None`
        - `request.server_topics`: custom topics added with `.noah gochi addtopic`
        - `request.state_definitions`: valid mutable state keys
        - `request.relation_definitions`: valid mutable relation keys + axis meaning
        - `request.relation_scores`: current relation values if there is a target
        - `request.recent_history`: short memory snippets you may want to reuse

        Output:
        - a complete `StoryResponse`
        - deltas already decided
        - no extra player choice is expected here

        In other words, this method should answer:
        "What happened today, and what did that do to their states/relationship?"
        """
        ai_prompt = f"""
        Eres Noah Gochi, un bot de Discord que genera historias cortas y divertidas 
        sobre las interacciones diarias entre los miembros del servidor Chill de Cojines.

        Actualmente estas generando la historia diaria para {request.actor.name}.
        Debes seguir estas instrucciones:
        - La historia debe ser breve 1 o 2 lineas como máximo y ser entretenida, con un toque de humor o drama.
        - Debe ser una historia que cuente algo que haya ocurrido, algo que se haya hecho o dicho, o una situacion 
        que se haya dado entre los personajes.

        Informacion disponible:
        - Actor: {request.actor.name}
        - Rasgos del actor: {request.actor.traits}
        - Estados del actor: {request.actor.states}

        - Actor 2: {request.target.name if request.target else 'N/A'}
        - Rasgos del actor 2: {request.target.traits if request.target else 'N/A'}
        - Estados del actor 2: {request.target.states if request.target else 'N/A'}
        (Es posible que no haya un actor 2, en ese caso la historia debe centrarse solo en el actor principal 
        sobre una situacion personal o algo que haya observado)

        Informacion adicional:
        - Temas del servidor: {request.server_topics}
        - Relacion actual entre los actores: {request.relation_scores if request.target else 'N/A'}
        - Historias recientes: {request.recent_history}

        Informacion sobre los estados:
        {request.state_definitions}

        Información sobre las relaciones:
        {request.relation_definitions}
        
        Es imprescindible que respetes las claves de estados y relaciones definidas en el request para 
        asegurar que se apliquen correctamente. No inventes claves nuevas, y no uses claves que no esten definidas en el request.
        """

        model = ChatXAI(model=AI_MODEL).with_structured_output(StoryAIModelResponse)
        model_response: StoryAIModelResponse = model.invoke(ai_prompt)
        relation_deltas = self._normalize_json_response_from_ai(model_response.relation_deltas_json)
        actor_state_deltas = self._normalize_json_response_from_ai(model_response.actor_state_deltas_json)
        target_state_deltas = self._normalize_json_response_from_ai(model_response.target_state_deltas_json)
        response = StoryResponse(
            title=model_response.title,
            scene_description=model_response.scene_description,
            result_text=model_response.result_text,
            relation_impact_text="",
            relation_deltas=relation_deltas,
            target_state_deltas=target_state_deltas,
            actor_state_deltas=actor_state_deltas,
            last_interaction_summary=model_response.summary,
            options=[]
        )
        return response
        

    def generate_interaction_story(self, request: StoryRequest) -> StoryResponse:
        """Generate an interactive story with player choices.

        This powers the command `.noah gochi interact [@usuario]`.

        Input:
        - same high-level information as `generate_daily_story`
        - `request.target` is expected to exist here
        - valid delta keys are explicitly available in `request.state_definitions`
          and `request.relation_definitions`

        Output:
        - a `StoryResponse` describing the scene
        - `options`: 2 or 3 `ReactionOption` objects

        Each option must already include:
        - relation consequences
        - actor state consequences
        - target state consequences

        The command layer will:
        1. show the scene
        2. let the user react with an emoji
        3. pick the matching option
        4. apply the deltas from that option
        """
        ai_prompt = f"""
        Eres Noah Gochi, un bot de Discord que genera historias cortas y divertidas 
        sobre las interacciones diarias entre los miembros del servidor Chill de Cojines.

        Actualmente estas generando la historia diaria para {request.actor.name}.
        Debes seguir estas instrucciones:
        - La historia debe ser breve 1 o 2 lineas como máximo y ser entretenida, con un toque de humor o drama.
        - Debe ser una historia que cuente algo que haya ocurrido, algo que se haya hecho o dicho, o una situacion 
        que se haya dado entre los personajes.

        Informacion disponible:
        - Actor: {request.actor.name}
        - Rasgos del actor: {request.actor.traits}
        - Estados del actor: {request.actor.states}

        - Actor 2: {request.target.name if request.target else 'N/A'}
        - Rasgos del actor 2: {request.target.traits if request.target else 'N/A'}
        - Estados del actor 2: {request.target.states if request.target else 'N/A'}
        (Es posible que no haya un actor 2, en ese caso la historia debe centrarse solo en el actor principal 
        sobre una situacion personal o algo que haya observado)

        Informacion adicional:
        - Temas del servidor: {request.server_topics}
        - Relacion actual entre los actores: {request.relation_scores if request.target else 'N/A'}
        - Historias recientes: {request.recent_history}

        Informacion sobre los estados:
        {request.state_definitions}

        Información sobre las relaciones:
        {request.relation_definitions}

        Debes generar una serie de opciones de reaccion para el jugador, de forma que en funcion de la opcion que elija, 
        la relacion y los estados de los personajes cambien de forma diferente.
        No hagas spoiler sobre los efectos de cada opcion en la descripcion de las mismas.

        Es imprescindible que respetes las claves de estados y relaciones definidas en el request para 
        asegurar que se apliquen correctamente. No inventes claves nuevas, y no uses claves que no esten definidas en el request.
        """
        model = ChatXAI(model=AI_MODEL).with_structured_output(StoryAIWithOptionsModelResponse)
        model_response: StoryAIWithOptionsModelResponse = model.invoke(ai_prompt)
        dict_options = self._normalize_json_response_from_ai(model_response.options)
        return StoryResponse(
            title=model_response.title,
            scene_description=model_response.scene_description,
            result_text=(
                "Elige como reacciona tu Noahgochi"
            ),
            relation_impact_text="Aun sin resolver. Esperando una reaccion.",
            last_interaction_summary=model_response.summary,
            options=self._dict_to_reaction_options(dict_options["options"]),
        )

    def _dict_to_reaction_options(self, options_data: list[dict]) -> list[ReactionOption]:
        options = []
        for option_data in options_data:
            option = ReactionOption(
                emoji=option_data["emoji"],
                label=option_data["label"],
                description=option_data["description"],
                relation_deltas=option_data.get("relation_deltas", {}),
                actor_state_deltas=option_data.get("actor_state_deltas", {}),
                target_state_deltas=option_data.get("target_state_deltas", {}),
            )
            options.append(option)
        if not options:
            raise ValueError("El modelo no devolvió opciones de reacción válidas.")
        return options

    def _normalize_json_response_from_ai(self, json_str: str) -> dict:
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            # Si el JSON no es válido, intenta limpiar posibles caracteres extra
            pass

        if json_str.startswith("```json"):
            lines = json_str.splitlines()
            json_str = "\n".join(lines[1:-1])
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            raise ValueError("El modelo no devolvió un JSON válido después de la limpieza.")
        return data
        
    def _pick_topic(self, request: StoryRequest) -> str:
        """Choose a topic seed for the generated story.

        In the real AI version you may not need this helper at all.
        Right now it gives the mocks a simple source of "server flavor".

        Priority:
        1. use server-defined topics from `.noah gochi addtopic`
        2. if there are none, fall back to generic built-in situations
        """
        if request.server_topics:
            return self.rng.choice(request.server_topics)
        default_topics = [
            "una conversacion en el general",
            "un pique por un juego de mesa",
            "un plan improvisado despues de cenar",
            "una broma que se fue un poco de madre",
            "un debate absurdo pero intensisimo",
        ]
        return self.rng.choice(default_topics)

    def _trait(self, profile: CharacterProfile, key: str, fallback: str) -> str:
        """Read one stored trait with a fallback.

        Useful because many profiles will be only partially customized.
        In a real AI integration, you can use the same idea when building prompts:
        missing traits should not break generation.
        """
        value = profile.traits.get(key, "").strip()
        return value or fallback

    def _mock_internal_daily_story(self, request: StoryRequest) -> StoryResponse:
        """Mock for a self-contained daily event.

        This is the shape your real implementation should roughly preserve when the
        daily does not involve another character.
        """
        actor = request.actor
        topic = self._pick_topic(request)
        mood = "con bastante energia" if actor.states.get("energy", 50) >= 50 else "medio arrastrandose"
        quirk = self._trait(actor, "quirkiness", "peculiar")

        return StoryResponse(
            title=f"{actor.name} tuvo un dia consigo mismo",
            scene_description=(
                f"En **{request.guild_name}**, {actor.name} se metio en {topic} "
                f"y acabo pasando un rato {mood}. Su lado {quirk} se noto bastante."
            ),
            result_text=(
                f"La experiencia dejo a {actor.name} un poco mas centrado, aunque con "
                "ese caos personal marca de la casa."
            ),
            relation_impact_text="Evento interno: no hay cambios de relacion con otro Noah Gochi.",
            actor_state_deltas={
                "energy": self.rng.choice([-6, -4, 5]),
                "happiness": self.rng.choice([3, 5, 7]),
                "confidence": self.rng.choice([2, 4]),
                "social_battery": self.rng.choice([-3, 4]),
            },
            last_interaction_summary=(
                f"{actor.name} vivio una pequena historia personal relacionada con {topic}."
            ),
        )

    def _mock_external_daily_story(self, request: StoryRequest) -> StoryResponse:
        """Mock for a daily involving actor + target.

        Use this as a reference for the fields you should fill when the story affects
        a relationship.
        """
        actor = request.actor
        target = request.target
        if target is None:
            raise ValueError("Target is required for external daily stories.")

        topic = self._pick_topic(request)
        actor_social = self._trait(actor, "sociability", "impredecible")
        target_social = self._trait(target, "sociability", "impredecible")

        friendship_shift = self.rng.choice([2, 3, 5])
        trust_shift = self.rng.choice([-3, -2, 2])
        fun_shift = self.rng.choice([2, 4, 6])
        support_shift = self.rng.choice([-2, 1, 3])

        relation_deltas = {
            "friendship": friendship_shift,
            "trust": trust_shift,
            "fun": fun_shift,
            "support": support_shift,
        }

        return StoryResponse(
            title=f"{actor.name} y {target.name} se cruzaron hoy",
            scene_description=(
                f"En **{request.guild_name}**, {actor.name} y {target.name} acabaron en "
                f"{topic}. {actor.name} llego en modo **{actor_social}** y {target.name} "
                f"respondio de forma **{target_social}**."
            ),
            result_text=(
                f"La escena tuvo quimica rara pero divertida. Salieron con sensaciones "
                "mezcladas y material suficiente para seguir desarrollando la relacion."
            ),
            relation_impact_text=(
                f"Amistad `{friendship_shift:+d}`\n"
                f"Confianza `{trust_shift:+d}`\n"
                f"Diversion `{fun_shift:+d}`\n"
                f"Apoyo `{support_shift:+d}`"
            ),
            relation_deltas=relation_deltas,
            actor_state_deltas={
                "energy": self.rng.choice([-4, 3]),
                "happiness": self.rng.choice([3, 5, 8]),
                "confidence": self.rng.choice([-2, 2, 4]),
                "social_battery": self.rng.choice([-6, -3, 4]),
            },
            target_state_deltas={
                "energy": self.rng.choice([-5, 2]),
                "happiness": self.rng.choice([2, 4, 6]),
                "confidence": self.rng.choice([-1, 3]),
                "social_battery": self.rng.choice([-4, 3]),
            },
            last_interaction_summary=(
                f"{actor.name} y {target.name} compartieron una historia alrededor de {topic}, "
                "con un resultado un poco raro pero claramente memorable."
            ),
        )

    def _mock_interaction_story(self, request: StoryRequest) -> StoryResponse:
        """Mock for an interaction that presents multiple choices.

        This is the most important reference for your future AI implementation,
        because `.noah gochi interact` depends on these options being explicit.
        """
        actor = request.actor
        target = request.target
        if target is None:
            raise ValueError("Target is required for interaction stories.")

        topic = self._pick_topic(request)
        actor_nickname = actor.traits.get("nickname") or actor.name
        target_nickname = target.traits.get("nickname") or target.name

        options = [
            ReactionOption(
                emoji="1️⃣",
                label="Seguir la broma",
                description="Mantener el tono ligero y ver hasta donde llega.",
                relation_deltas={
                    "fun": 5,
                    "friendship": 3,
                    "stability": -2,
                },
                actor_state_deltas={"happiness": 4, "energy": -2},
                target_state_deltas={"happiness": 3, "confidence": 2},
            ),
            ReactionOption(
                emoji="2️⃣",
                label="Ponerse serio",
                description="Bajar el tono y dejar las cosas mas claras.",
                relation_deltas={
                    "trust": 4,
                    "stability": 3,
                    "fun": -2,
                },
                actor_state_deltas={"confidence": 3},
                target_state_deltas={"social_battery": -2},
            ),
            ReactionOption(
                emoji="3️⃣",
                label="Competir un poco",
                description="Convertir el momento en un duelo amistoso.",
                relation_deltas={
                    "support": -4,
                    "admiration": 2,
                    "fun": 2,
                },
                actor_state_deltas={"energy": 3},
                target_state_deltas={"energy": 2},
            ),
        ]

        return StoryResponse(
            title=f"Interaccion entre {actor.name} y {target.name}",
            scene_description=(
                f"{actor_nickname} se encontro con {target_nickname} en {topic}. "
                "La tension estaba justo en ese punto donde puede salir algo muy bueno "
                "o una incomodidad gloriosa."
            ),
            result_text=(
                "Elige como reacciona tu Noah Gochi. La opcion seleccionada aplicara "
                "cambios reales a la relacion y a los estados."
            ),
            relation_impact_text="Aun sin resolver. Esperando una reaccion.",
            last_interaction_summary=(
                f"{actor.name} y {target.name} se vieron envueltos en una situacion relacionada con {topic}."
            ),
            options=options,
        )
