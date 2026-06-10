# Changelog

## [0.8.0](https://github.com/ChosunUniv2026Capstone/Backend/compare/v0.7.0...v0.8.0) (2026-06-10)


### Features

* **backend:** enable continuous automatic attendance ([47e6141](https://github.com/ChosunUniv2026Capstone/Backend/commit/47e61413bb055077e864e95a7381b0d1566b4b9a))

## [0.7.0](https://github.com/ChosunUniv2026Capstone/Backend/compare/v0.6.0...v0.7.0) (2026-05-22)


### Features

* **backend:** support explicit assignment attachment removal ([95465c1](https://github.com/ChosunUniv2026Capstone/Backend/commit/95465c1aa6241c5ea2271fb64fe9a0976c0d0136))

## [0.6.0](https://github.com/ChosunUniv2026Capstone/Backend/compare/v0.5.3...v0.6.0) (2026-05-21)


### Features

* **backend:** let professors export attendance CSVs ([36e8524](https://github.com/ChosunUniv2026Capstone/Backend/commit/36e852483cf97876a12f93c0dbdf59d783c4d607))


### Bug Fixes

* **backend:** prevent presence waits from starving DB connections ([7f02a87](https://github.com/ChosunUniv2026Capstone/Backend/commit/7f02a87bea61f69d5778c63508505ff47fcd9a9b))

## [0.5.3](https://github.com/ChosunUniv2026Capstone/Backend/compare/v0.5.2...v0.5.3) (2026-05-18)


### Bug Fixes

* require attendance reasons only for official status ([b24d0f4](https://github.com/ChosunUniv2026Capstone/Backend/commit/b24d0f4123bf07dd818df8f4ada2e809082eca54))

## [0.5.2](https://github.com/ChosunUniv2026Capstone/Backend/compare/v0.5.1...v0.5.2) (2026-05-17)


### Bug Fixes

* allow professors to recover smart attendance after manual starts ([bd7cc09](https://github.com/ChosunUniv2026Capstone/Backend/commit/bd7cc094ccc0f4427ad4d060ffa4423f7101d030))
* preserve manual bundle scope during smart recovery ([d8dbb61](https://github.com/ChosunUniv2026Capstone/Backend/commit/d8dbb61d98b4f7599ff7f5b393a48ee4de48e36d))

## [0.5.1](https://github.com/ChosunUniv2026Capstone/Backend/compare/v0.5.0...v0.5.1) (2026-05-17)


### Bug Fixes

* **backend:** normalize presence dependency outages ([f85810b](https://github.com/ChosunUniv2026Capstone/Backend/commit/f85810b2e1f03433c978b37f86ba7e0b22af90b1))

## [0.5.0](https://github.com/ChosunUniv2026Capstone/Backend/compare/v0.4.1...v0.5.0) (2026-05-16)


### Features

* **backend:** align API responses with envelope contract ([91d5c49](https://github.com/ChosunUniv2026Capstone/Backend/commit/91d5c491b9bac85fff2fd61e3a49aa1f10a33ca6))
* **backend:** enable selected LMS demo APIs ([ec7a5f5](https://github.com/ChosunUniv2026Capstone/Backend/commit/ec7a5f503ddf39c767365a7ad0d3ee413a5c1425))
* **backend:** forward admin snapshot source ([ad5e59d](https://github.com/ChosunUniv2026Capstone/Backend/commit/ad5e59d5961e648eac73f384aeddde53cdcf7af7))
* **backend:** own AP token registry for collector push ([31d18f3](https://github.com/ChosunUniv2026Capstone/Backend/commit/31d18f3f566cded964c464a64f7770f8a800b00f))
* **backend:** pass admin snapshot refresh through ([723af07](https://github.com/ChosunUniv2026Capstone/Backend/commit/723af07999709312367774eaf2ae8c64f43281de))

## [0.4.1](https://github.com/ChosunUniv2026Capstone/Backend/compare/v0.4.0...v0.4.1) (2026-05-14)


### Chores

* refresh package version after live full-feature E2E validation

## [0.4.0](https://github.com/ChosunUniv2026Capstone/Backend/compare/v0.3.0...v0.4.0) (2026-05-10)


### Features

* **backend:** enforce proxy-owned object storage ([7b9bc9f](https://github.com/ChosunUniv2026Capstone/Backend/commit/7b9bc9fd30ffa1d1b0dbea39fcf270326258137a))

## [0.3.0](https://github.com/ChosunUniv2026Capstone/Backend/compare/v0.2.0...v0.3.0) (2026-05-10)


### Features

* **backend:** implement assignment workflow api ([c2eb732](https://github.com/ChosunUniv2026Capstone/Backend/commit/c2eb732b3135f76fb4c61a0bcf5f46194a66e741))

## [0.2.0](https://github.com/ChosunUniv2026Capstone/Backend/compare/v0.1.0...v0.2.0) (2026-04-26)


### Features

* **backend:** add notice detail endpoint ([#1](https://github.com/ChosunUniv2026Capstone/Backend/issues/1)) ([1cebcc3](https://github.com/ChosunUniv2026Capstone/Backend/commit/1cebcc39aaa1a113dc1c94d4ccc8cc63ba984b31))
* **backend:** finalize exam workflow ([9273d6b](https://github.com/ChosunUniv2026Capstone/Backend/commit/9273d6b5256ffbccaa708a450d40dad88005f405))
* **backend:** publish release images from component releases ([#10](https://github.com/ChosunUniv2026Capstone/Backend/issues/10)) ([65349f2](https://github.com/ChosunUniv2026Capstone/Backend/commit/65349f2a1ba2336b8a68777162f7bb27e676fc97))
* **backend:** resolve current-course presence checks server-side ([483d7e2](https://github.com/ChosunUniv2026Capstone/Backend/commit/483d7e2990c06c766a9a70aaf83e59ec450ba7d8))
* **backend:** secure demo flows behind reusable guards ([65584b7](https://github.com/ChosunUniv2026Capstone/Backend/commit/65584b72a3bc764e458ebbb57890fb4c716eceaa))


### Bug Fixes

* **backend:** finalize attendance state and cookie auth flows ([25a99d6](https://github.com/ChosunUniv2026Capstone/Backend/commit/25a99d6f411c4108a116b25e525fbeae3414b448))
* **backend:** freeze current-course presence contracts ([8112e0e](https://github.com/ChosunUniv2026Capstone/Backend/commit/8112e0e823b4fc39270885a6ef468204b1cfd74c))
* **backend:** preserve attendance session restore and course-scoped auth ([32ebc0e](https://github.com/ChosunUniv2026Capstone/Backend/commit/32ebc0e20708a92389ed3943b3e4187b88641818))

## 0.1.0

- Initial demo component release baseline.
