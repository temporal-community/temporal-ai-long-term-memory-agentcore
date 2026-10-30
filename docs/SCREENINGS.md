# Sample screenings

Nicole's default schedule is derived from cinema listings captured in October
2026 at Grand Lake Theatre, the New Parkway, and AMC Bay Street 16. It contains
53 unique showings for 13 films and events. Identical combinations of film,
cinema, local time, and format from the captured weekend are merged.

The [sample data](../src/nicole/data/cinema_samples.json) preserves film metadata,
cinema coordinates, formats, and local start times. On lookup, Nicole puts those
times on the requested date in `America/Los_Angeles` and filters by time and
radius. Refreshing a selected showing generates the same stable ID and schedule.
This works in later years without a showtimes account.

The adapted dates are fictional. Cards are labeled “Sample screening,” and saved
plans identify the showing as demo data. Booking links are omitted because the
adapted showing is not a published event. No availability, seat reservation, or
purchase is implied.

## Sources

The file retains capture timestamps, the source dates, and source URLs for each
showing. The original programs came from:

- [Grand Lake Theatre](https://www.renaissancerialto.com/)
- [The New Parkway Theater](https://thenewparkway.com/)
- [AMC Bay Street 16](https://www.amctheatres.com/movie-theatres/san-francisco/amc-bay-street-16/showtimes)

These sources document the captured material, not the adapted demo dates. To
change the sample selection, edit the date-independent records in
`cinema_samples.json` and keep their provenance.

## Live listings

Start a new visit with `--live-showtimes` and configure `SHOWTIMES_API_KEY` for
International Showtimes. This is a separate provider integration; the sample
schedule never silently falls back to it. Live Oakland coverage has not been
verified by this project.
