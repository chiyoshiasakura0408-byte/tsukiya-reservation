"""Availability marks use remaining inventory before the requested booking."""


def marks(app, connection, day, slots):
    result = {'COUNTER': {}, 'PRIVATE': {}}
    for clock, round_number in (('18:00', 1), ('20:30', 2)):
        if slots['COUNTER'][clock]:
            remaining = next((size for size in range(app.COUNTER_CAPACITY, 0, -1)
                              if app.availability_check(connection, 'COUNTER',
                                  str(day)+'T'+clock, size, round_number)[0]), 0)
            result['COUNTER'][clock] = '△' if remaining <= 4 else '○'
        else:
            result['COUNTER'][clock] = '×'
        if any(slots[room][clock] for room in app.ROOMS):
            # Count physical vacant rooms, including the room with a four-person minimum.
            # The selected party's eligibility is already enforced by slots above.
            remaining = sum(app.availability_check(connection, room, str(day)+'T'+clock,
                            next(size for size in range(2, 9) if app.public_party_allowed(room, size)))[0]
                            for room in app.ROOMS)
            result['PRIVATE'][clock] = '△' if remaining <= 2 else '○'
        else:
            result['PRIVATE'][clock] = '×'
    return result
