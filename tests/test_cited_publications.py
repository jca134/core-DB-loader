"""
Tests for the grant-cited paper backfill: parsing PubMed efetch XML, and the
filovirus filter that decides which of ~20k cited papers enter the KG.
No network, no database.
"""

import xml.etree.ElementTree as ET

from core_data_loader.etl.etl_evidence import is_filovirus_paper
from core_data_loader.loaders.fetch_cited_publications import parse_article

ARTICLE = """
<PubmedArticle>
  <MedlineCitation>
    <PMID>12345</PMID>
    <Article>
      <Journal>
        <JournalIssue><PubDate><MedlineDate>2014 Jun-Jul</MedlineDate></PubDate></JournalIssue>
        <Title>Journal of Virology</Title>
      </Journal>
      <ArticleTitle>Structure of the <i>Ebola virus</i> glycoprotein.</ArticleTitle>
      <Abstract>
        <AbstractText Label="BACKGROUND">GP mediates <i>entry</i>.</AbstractText>
        <AbstractText Label="RESULTS">We solved it.</AbstractText>
      </Abstract>
      <AuthorList>
        <Author><LastName>Saphire</LastName><Initials>EO</Initials></Author>
        <Author><CollectiveName>Viral Hemorrhagic Fever Consortium</CollectiveName></Author>
      </AuthorList>
    </Article>
    <MeshHeadingList>
      <MeshHeading><DescriptorName>Ebolavirus</DescriptorName></MeshHeading>
      <MeshHeading><DescriptorName>Viral Envelope Proteins</DescriptorName></MeshHeading>
    </MeshHeadingList>
  </MedlineCitation>
  <PubmedData>
    <ArticleIdList>
      <ArticleId IdType="pubmed">12345</ArticleId>
      <ArticleId IdType="doi">10.1/abc</ArticleId>
    </ArticleIdList>
  </PubmedData>
</PubmedArticle>
"""


def test_parse_article_reads_every_field():
    rec = parse_article(ET.fromstring(ARTICLE))
    assert rec["pmid"] == "12345"
    # Nested markup is kept as text, and the trailing period dropped.
    assert rec["title"] == "Structure of the Ebola virus glycoprotein"
    # Structured abstracts keep their section labels.
    assert rec["abstract"] == "BACKGROUND: GP mediates entry. RESULTS: We solved it."
    assert rec["mesh_terms"] == "Ebolavirus; Viral Envelope Proteins"
    # MedlineDate has no <Year>; the leading year is still recovered.
    assert rec["year"] == "2014"
    assert rec["authors"] == "Saphire EO; Viral Hemorrhagic Fever Consortium"
    assert rec["doi"] == "10.1/abc"
    assert rec["journal"] == "Journal of Virology"


def test_parse_article_tolerates_missing_abstract_and_mesh():
    xml = ARTICLE.replace("<Abstract>", "<!--").replace("</Abstract>", "-->")
    xml = xml.replace("<MeshHeadingList>", "<!--").replace("</MeshHeadingList>", "-->")
    rec = parse_article(ET.fromstring(xml))
    assert rec["abstract"] == "" and rec["mesh_terms"] == ""


def test_title_mention_is_enough():
    assert is_filovirus_paper("Marburg virus VP40 budding", "", "")


def test_mesh_heading_is_enough():
    # The title doesn't name the virus; the indexers did.
    assert is_filovirus_paper("NPC1 is an entry receptor", "Ebolavirus; Humans", "")


def test_single_abstract_mention_is_a_passing_reference():
    abstract = "PTC299 also blocked replication of other RNA viruses, including Ebola virus."
    assert not is_filovirus_paper("A DHODH inhibitor arrests SARS-CoV-2", "", abstract)


def test_two_abstract_mentions_are_enough():
    abstract = "We isolated an antibody against EBOV GP. It neutralised Ebola-virus in vitro."
    assert is_filovirus_paper("A broadly neutralising antibody", "", abstract)


def test_unrelated_paper_is_excluded():
    assert not is_filovirus_paper("Sclerosing cholangitis in baboons", "Papio; Liver", "No virus.")
